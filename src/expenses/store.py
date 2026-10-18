"""Persistent analysis database: schema and merge of backups (see DESIGN.md section 3)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import fx
from .source import SourceData, SourceRecord

DATA_FIELDS = (
    "date", "account", "account_currency", "amount", "original_currency", "original_amount",
    "category_path", "category_top", "category_sub", "payee", "comment", "method", "tags",
    "is_transfer",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    uuid TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    account TEXT NOT NULL,
    account_currency TEXT NOT NULL,
    amount REAL NOT NULL,
    original_currency TEXT,
    original_amount REAL,
    category_path TEXT,
    category_top TEXT,
    category_sub TEXT,
    payee TEXT,
    comment TEXT,
    method TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    is_transfer INTEGER NOT NULL DEFAULT 0,
    first_seen_backup TEXT,
    last_seen_backup TEXT,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions (date);
CREATE TABLE IF NOT EXISTS transaction_tags (
    uuid TEXT NOT NULL,
    tag TEXT NOT NULL,
    PRIMARY KEY (uuid, tag)
);
CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    backup_file TEXT NOT NULL,
    backup_timestamp TEXT,
    imported_at TEXT NOT NULL,
    min_date TEXT,
    max_date TEXT,
    inserted INTEGER NOT NULL,
    updated INTEGER NOT NULL,
    unchanged INTEGER NOT NULL,
    kept_missing INTEGER NOT NULL,
    dropped INTEGER NOT NULL
);
"""


@dataclass
class MergeResult:
    inserted: int = 0
    updated: int = 0  # includes revived rows
    unchanged: int = 0
    kept_missing: int = 0
    dropped: int = 0
    revived: int = 0
    skipped: bool = False
    skip_reason: str | None = None  # "not_newer" | "empty_source"
    empty_source: bool = False  # suspicious: nothing was changed


def open_db(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = OFF")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    fx.ensure_schema(conn)


def _normalize_ts(value: datetime) -> str:
    """ISO string in UTC without offset, so naive and aware values compare consistently."""
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.isoformat(timespec="seconds")


def _values(rec: SourceRecord) -> dict[str, object]:
    return {
        "date": rec.date,
        "account": rec.account,
        "account_currency": rec.account_currency,
        "amount": rec.amount,
        "original_currency": rec.original_currency,
        "original_amount": rec.original_amount,
        "category_path": rec.category_path,
        "category_top": rec.category_top,
        "category_sub": rec.category_sub,
        "payee": rec.payee,
        "comment": rec.comment,
        "method": rec.method,
        "tags": json.dumps(sorted(rec.tags), ensure_ascii=False),
        "is_transfer": int(rec.is_transfer),
    }


def _sync_tags(conn: sqlite3.Connection, uuid: str, tags: list[str]) -> None:
    conn.execute("DELETE FROM transaction_tags WHERE uuid = ?", (uuid,))
    conn.executemany(
        "INSERT OR IGNORE INTO transaction_tags (uuid, tag) VALUES (?, ?)",
        [(uuid, t) for t in sorted(set(tags))],
    )


def _latest_import(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT MAX(backup_timestamp) FROM imports WHERE backup_timestamp IS NOT NULL"
    ).fetchone()
    return row[0]


def merge(
    conn: sqlite3.Connection,
    source: SourceData,
    backup_name: str,
    backup_timestamp: datetime | None = None,
    force: bool = False,
) -> MergeResult:
    """Merge a backup into the analysis DB in a single transaction."""
    ensure_schema(conn)
    result = MergeResult()
    ts = _normalize_ts(backup_timestamp) if backup_timestamp else None

    if not source.records:
        # an empty backup would otherwise soft-delete everything: refuse
        result.skipped = result.empty_source = True
        result.skip_reason = "empty_source"
        return result
    if not force and ts is not None:
        latest = _latest_import(conn)
        if latest is not None and ts <= latest:
            result.skipped = True
            result.skip_reason = "not_newer"
            return result

    min_date = source.min_date
    now = _normalize_ts(datetime.now(UTC))
    try:
        conn.execute("BEGIN")
        existing = {
            r["uuid"]: r
            for r in conn.execute(
                "SELECT uuid, date, deleted_at, " + ", ".join(DATA_FIELDS) + " FROM transactions"
            )
        }
        seen: set[str] = set()
        for rec in source.records:
            seen.add(rec.uuid)
            values = _values(rec)
            old = existing.get(rec.uuid)
            if old is None:
                cols = ["uuid", *values, "first_seen_backup", "last_seen_backup"]
                conn.execute(
                    f"INSERT INTO transactions ({', '.join(cols)}) "
                    f"VALUES ({', '.join('?' * len(cols))})",
                    [rec.uuid, *values.values(), backup_name, backup_name],
                )
                _sync_tags(conn, rec.uuid, rec.tags)
                result.inserted += 1
                continue
            revived = old["deleted_at"] is not None
            changed = revived or any(old[k] != v for k, v in values.items())
            if changed:
                sets = ", ".join(f"{k} = ?" for k in values)
                conn.execute(
                    f"UPDATE transactions SET {sets}, last_seen_backup = ?, deleted_at = NULL "
                    "WHERE uuid = ?",
                    [*values.values(), backup_name, rec.uuid],
                )
                _sync_tags(conn, rec.uuid, rec.tags)
                result.updated += 1
                result.revived += revived
            else:
                conn.execute(
                    "UPDATE transactions SET last_seen_backup = ? WHERE uuid = ?",
                    (backup_name, rec.uuid),
                )
                result.unchanged += 1

        for uuid, old in existing.items():
            if uuid in seen or old["deleted_at"] is not None:
                continue
            if old["date"] < min_date:
                result.kept_missing += 1
            else:
                conn.execute("UPDATE transactions SET deleted_at = ? WHERE uuid = ?", (now, uuid))
                conn.execute("DELETE FROM transaction_tags WHERE uuid = ?", (uuid,))
                result.dropped += 1

        conn.execute(
            "INSERT INTO imports (backup_file, backup_timestamp, imported_at, min_date, max_date,"
            " inserted, updated, unchanged, kept_missing, dropped) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (backup_name, ts, now, min_date, source.max_date, result.inserted, result.updated,
             result.unchanged, result.kept_missing, result.dropped),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return result
