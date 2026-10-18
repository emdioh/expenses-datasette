import os
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from expenses.backup import (
    BackupError,
    archive_backup,
    decrypt_backup,
    encrypt_backup,
    find_latest_backup,
    open_backup,
    parse_backup_timestamp,
    read_home_currency,
)

PREFS = '<?xml version="1.0"?><map><string name="home_currency">EUR</string><boolean name="x" value="true"/></map>'


def make_zip(path: Path, extra: dict[str, bytes] | None = None, with_db: bool = True) -> Path:
    db = path.parent / "_src.sqlite"
    db.unlink(missing_ok=True)
    con = sqlite3.connect(db)
    con.execute("create table t (a integer)")
    con.execute("insert into t values (42)")
    con.commit()
    con.close()
    with zipfile.ZipFile(path, "w") as zf:
        if with_db:
            zf.write(db, "BACKUP")
        zf.writestr("BACKUP_PREF", PREFS)
        for name, content in (extra or {}).items():
            zf.writestr(name, content)
    db.unlink()
    return path


def test_parse_timestamp():
    assert parse_backup_timestamp(Path("backup-20240102-030405.zip")) == datetime(2024, 1, 2, 3, 4, 5)
    assert parse_backup_timestamp(Path("my-prefix-20240102-030405.enc")) == datetime(2024, 1, 2, 3, 4, 5)
    assert parse_backup_timestamp(Path("random.zip")) is None
    assert parse_backup_timestamp(Path("backup-20241302-030405.zip")) is None


def test_find_latest_by_name_and_prefix(tmp_path):
    for n in ["backup-20230101-000000.zip", "other-20240601-120000.enc", "backup-20240101-000000.zip", "notes.txt"]:
        (tmp_path / n).write_bytes(b"x")
    # make the newest-by-name file oldest by mtime
    os.utime(tmp_path / "other-20240601-120000.enc", (1, 1))
    assert find_latest_backup(tmp_path).name == "other-20240601-120000.enc"


def test_find_latest_none(tmp_path):
    (tmp_path / "readme.txt").write_text("x")
    with pytest.raises(BackupError):
        find_latest_backup(tmp_path)
    with pytest.raises(BackupError):
        find_latest_backup(tmp_path / "missing")


def test_encrypt_decrypt_roundtrip():
    assert decrypt_backup(encrypt_backup(b"hello", "pw"), "pw") == b"hello"


def test_wrong_password_and_tamper():
    enc = encrypt_backup(b"hello", "secret-pw")
    with pytest.raises(BackupError, match="wrong password") as ei:
        decrypt_backup(enc, "other")
    assert "secret-pw" not in str(ei.value) and "other" not in str(ei.value)
    tampered = enc[:-1] + bytes([enc[-1] ^ 1])
    with pytest.raises(BackupError, match="wrong password or corrupted"):
        decrypt_backup(tampered, "secret-pw")


def test_bad_magic():
    with pytest.raises(BackupError, match="not a MyExpenses encrypted backup"):
        decrypt_backup(b"XX_ENC_01" + b"\0" * 40, "pw")
    with pytest.raises(BackupError, match="not a MyExpenses"):
        decrypt_backup(b"", "pw")


def test_open_zip(tmp_path):
    z = make_zip(tmp_path / "backup-20240102-030405.zip")
    with open_backup(z) as b:
        assert b.timestamp == datetime(2024, 1, 2, 3, 4, 5)
        assert b.source_file == z
        assert sqlite3.connect(b.db_path).execute("select a from t").fetchone() == (42,)
        assert read_home_currency(b.prefs_path) == "EUR"
        tmp = b.db_path.parent
    assert not tmp.exists()


def test_open_enc_roundtrip(tmp_path):
    z = make_zip(tmp_path / "src.zip")
    e = tmp_path / "backup-20240102-030405.enc"
    e.write_bytes(encrypt_backup(z.read_bytes(), "pw"))
    with open_backup(e, "pw") as b:
        assert sqlite3.connect(b.db_path).execute("select a from t").fetchone() == (42,)
    with pytest.raises(BackupError, match="password"):
        with open_backup(e):
            pass
    with pytest.raises(BackupError, match="wrong password"):
        with open_backup(e, "nope"):
            pass


def test_missing_backup_member(tmp_path):
    z = make_zip(tmp_path / "backup-20240102-030405.zip", with_db=False)
    with pytest.raises(BackupError, match="BACKUP"):
        with open_backup(z):
            pass


def test_traversal_member_ignored(tmp_path):
    z = make_zip(
        tmp_path / "backup-20240102-030405.zip",
        extra={"../evil.txt": b"x", "/abs/evil.txt": b"x", "sub/BACKUP": b"junk"},
    )
    with open_backup(z) as b:
        assert sqlite3.connect(b.db_path).execute("select a from t").fetchone() == (42,)
        assert not (b.db_path.parent.parent / "evil.txt").exists()
        assert sorted(p.name for p in b.db_path.parent.iterdir()) == ["BACKUP", "BACKUP_PREF"]


def test_no_prefs(tmp_path):
    z = tmp_path / "backup-20240102-030405.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("BACKUP", b"not really sqlite")
    with open_backup(z) as b:
        assert b.prefs_path is None
        assert read_home_currency(b.prefs_path) is None


def test_read_home_currency(tmp_path):
    p = tmp_path / "pref.xml"
    p.write_text(PREFS)
    assert read_home_currency(p) == "EUR"
    p.write_text("<map><string name='other'>X</string></map>")
    assert read_home_currency(p) is None
    p.write_text("not xml")
    assert read_home_currency(p) is None


def test_archive_idempotent(tmp_path):
    src = tmp_path / "in" / "backup-20240102-030405.zip"
    src.parent.mkdir()
    make_zip(src)
    data = tmp_path / "data"
    dest = archive_backup(src, data)
    assert dest == data / "backups" / src.name
    assert dest.read_bytes() == src.read_bytes()
    os.utime(dest, (1, 1))
    assert archive_backup(src, data) == dest
    assert dest.stat().st_mtime == 1  # not re-copied
    dest.write_bytes(b"different")
    archive_backup(src, data)
    assert dest.read_bytes() == src.read_bytes()
