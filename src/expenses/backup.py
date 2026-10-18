"""Locate, decrypt and unpack MyExpenses backups."""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"ME_ENC_01"
IV_LEN = 12
DB_MEMBER = "BACKUP"
PREFS_MEMBER = "BACKUP_PREF"

_NAME_RE = re.compile(r"^.+-(\d{8})-(\d{6})\.(zip|enc)$")


class BackupError(Exception):
    """Raised for any problem locating, decrypting or reading a backup."""


@dataclass(frozen=True)
class OpenBackup:
    db_path: Path
    prefs_path: Path | None
    source_file: Path
    timestamp: datetime | None


def parse_backup_timestamp(path: Path) -> datetime | None:
    """Timestamp encoded in the file name, or None if it does not match."""
    m = _NAME_RE.match(Path(path).name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def find_latest_backup(backup_dir: Path) -> Path:
    """Newest `<prefix>-YYYYMMDD-HHMMSS.(zip|enc)`; ties/unparseable use mtime."""
    backup_dir = Path(backup_dir)
    if not backup_dir.is_dir():
        raise BackupError(f"backup directory not found: {backup_dir}")
    candidates = [
        p for p in backup_dir.iterdir() if p.is_file() and _NAME_RE.match(p.name)
    ]
    if not candidates:
        raise BackupError(f"no backup files found in {backup_dir}")

    def key(p: Path) -> tuple[datetime, float]:
        ts = parse_backup_timestamp(p)
        mtime = p.stat().st_mtime
        return (ts or datetime.fromtimestamp(mtime), mtime)

    return max(candidates, key=key)


def _key(password: str) -> bytes:
    return hashlib.sha256(password.encode("utf-8")).digest()


def decrypt_backup(data: bytes, password: str) -> bytes:
    """Decrypt a MyExpenses `.enc` payload into the plain zip bytes."""
    if not data.startswith(MAGIC) or len(data) < len(MAGIC) + IV_LEN + 16:
        raise BackupError("not a MyExpenses encrypted backup")
    iv = data[len(MAGIC) : len(MAGIC) + IV_LEN]
    body = data[len(MAGIC) + IV_LEN :]
    try:
        return AESGCM(_key(password)).decrypt(iv, body, None)
    except InvalidTag:
        raise BackupError("wrong password or corrupted backup") from None


def encrypt_backup(data: bytes, password: str, iv: bytes | None = None) -> bytes:
    """Inverse of decrypt_backup (used to build test fixtures)."""
    iv = iv if iv is not None else os.urandom(IV_LEN)
    return MAGIC + iv + AESGCM(_key(password)).encrypt(iv, data, None)


def _extract_member(zf: zipfile.ZipFile, name: str, dest_dir: Path) -> Path | None:
    """Extract exactly the member `name` to dest_dir/name, ignoring any path tricks."""
    try:
        info = zf.getinfo(name)
    except KeyError:
        return None
    if info.is_dir():
        return None
    target = dest_dir / name
    with zf.open(info) as src, open(target, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return target


@contextmanager
def open_backup(path: Path, password: str | None = None) -> Iterator[OpenBackup]:
    """Unpack a backup into a temp dir (removed on exit) and yield its parts."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise BackupError(f"cannot read backup {path.name}: {e.strerror}") from None
    if path.suffix.lower() == ".enc":
        if password is None:
            raise BackupError("backup is encrypted but no password was given")
        raw = decrypt_backup(raw, password)

    with tempfile.TemporaryDirectory(prefix="expenses-backup-") as tmp:
        tmp_dir = Path(tmp)
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                db = _extract_member(zf, DB_MEMBER, tmp_dir)
                prefs = _extract_member(zf, PREFS_MEMBER, tmp_dir)
        except zipfile.BadZipFile:
            raise BackupError("backup is not a valid zip archive") from None
        if db is None:
            raise BackupError(f"backup does not contain a {DB_MEMBER} file")
        yield OpenBackup(
            db_path=db,
            prefs_path=prefs,
            source_file=path,
            timestamp=parse_backup_timestamp(path),
        )


def read_home_currency(prefs_path: Path | None) -> str | None:
    """Value of the `home_currency` preference, or None. Reads nothing else."""
    if prefs_path is None:
        return None
    try:
        root = ET.parse(prefs_path).getroot()
    except (ET.ParseError, OSError):
        return None
    for el in root.iter("string"):
        if el.get("name") == "home_currency":
            value = (el.text or "").strip()
            return value or None
    return None


def archive_backup(path: Path, data_dir: Path) -> Path:
    """Copy the backup to data_dir/backups/ unless an identical copy exists."""
    path = Path(path)
    dest_dir = Path(data_dir) / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    if dest.exists():
        if dest.stat().st_size == path.stat().st_size and _digest(dest) == _digest(path):
            return dest
    shutil.copy2(path, dest)
    return dest


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
