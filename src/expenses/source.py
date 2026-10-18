"""Read a MyExpenses backup database into normalized source records."""

from __future__ import annotations

import hashlib
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SPLIT_LABEL = "__SPLIT_TRANSACTION__"
NULL_METHOD = "__NULL__"
SKIPPED_STATUSES = (2, 3, 4)
DEFAULT_FRACTION_DIGITS = 2


@dataclass
class SourceRecord:
    uuid: str
    date: str
    account: str
    account_currency: str
    amount: float
    original_currency: str | None
    original_amount: float | None
    category_path: str | None
    category_top: str | None
    category_sub: str | None
    payee: str | None
    comment: str | None
    method: str | None
    tags: list[str]
    is_transfer: bool


@dataclass
class SourceStats:
    read: int = 0  # records emitted
    skipped_by_status: dict[int, int] = field(default_factory=dict)
    splits_expanded: int = 0  # split parents replaced by their children
    missing_uuid: int = 0  # rows for which a key was synthesized


@dataclass
class SourceData:
    records: list[SourceRecord]
    stats: SourceStats

    @property
    def min_date(self) -> str | None:
        return min((r.date for r in self.records), default=None)

    @property
    def max_date(self) -> str | None:
        return max((r.date for r in self.records), default=None)


def _scaled(value: int | None, digits: int) -> float | None:
    return None if value is None else value / 10**digits


def _category_paths(conn: sqlite3.Connection) -> dict[int, list[str]]:
    rows = {
        r["_id"]: (r["label"], r["parent_id"])
        for r in conn.execute("SELECT _id, label, parent_id FROM categories")
    }
    paths: dict[int, list[str]] = {}
    for cid in rows:
        parts: list[str] = []
        seen: set[int] = set()
        cur: int | None = cid
        while cur is not None and cur in rows and cur not in seen:
            seen.add(cur)
            label, cur = rows[cur]
            parts.append(label)
        paths[cid] = parts[::-1]
    return paths


def _synthetic_uuid(parts: tuple[object, ...], occurrence: int) -> str:
    raw = "|".join(str(p) for p in (*parts, occurrence))
    return "noid-" + hashlib.sha1(raw.encode()).hexdigest()[:32]


def read_source(db_path: Path, tz: str = "Europe/Rome") -> SourceData:
    """Read all usable transactions of a MyExpenses BACKUP database (read-only)."""
    zone = ZoneInfo(tz)
    uri = f"file:{Path(db_path).resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return _read(conn, zone)
    finally:
        conn.close()


def _read(conn: sqlite3.Connection, zone: ZoneInfo) -> SourceData:
    digits = {
        r["code"]: r["fraction_digits"]
        for r in conn.execute("SELECT code, fraction_digits FROM currency")
        if r["fraction_digits"] is not None
    }
    accounts = {
        r["_id"]: (r["label"], r["currency"])
        for r in conn.execute("SELECT _id, label, currency FROM accounts")
    }
    cat_paths = _category_paths(conn)
    payees = {r["_id"]: r["name"] for r in conn.execute("SELECT _id, name FROM payee")}
    methods = {
        r["_id"]: r["label"] for r in conn.execute("SELECT _id, label FROM paymentmethods")
    }
    tags: dict[int, set[str]] = {}
    for r in conn.execute(
        "SELECT tt.transaction_id AS tid, t.label AS label "
        "FROM transactions_tags tt JOIN tags t ON t._id = tt.tag_id"
    ):
        tags.setdefault(r["tid"], set()).add(r["label"])

    txs = {r["_id"]: r for r in conn.execute("SELECT * FROM transactions ORDER BY _id")}
    children: dict[int, list[int]] = {}
    for tid, r in txs.items():
        if r["parent_id"] is not None:
            children.setdefault(r["parent_id"], []).append(tid)

    stats = SourceStats()
    skipped: Counter[int] = Counter()
    records: list[SourceRecord] = []
    occurrences: Counter[tuple[object, ...]] = Counter()

    for tid, row in txs.items():
        status = row["status"] or 0
        parent = txs.get(row["parent_id"]) if row["parent_id"] is not None else None
        if status in SKIPPED_STATUSES:
            skipped[status] += 1
            continue
        if parent is not None and (parent["status"] or 0) in (2, 3):
            skipped[parent["status"]] += 1
            continue
        if tid in children:  # split or archive parent: children carry the data
            if cat_paths.get(row["cat_id"]) == [SPLIT_LABEL]:
                stats.splits_expanded += 1
            continue

        account_id = row["account_id"]
        label, acc_currency = accounts[account_id]
        path = cat_paths.get(row["cat_id"], [])
        if path and path[0] == SPLIT_LABEL:
            path = []
        # Fall back to the parent for fields missing on a child.
        payee_id = row["payee_id"] if row["payee_id"] is not None else (
            parent["payee_id"] if parent else None
        )
        comment = row["comment"] or (parent["comment"] if parent else None) or None
        method_id = row["method_id"] if row["method_id"] is not None else (
            parent["method_id"] if parent else None
        )
        method = methods.get(method_id)
        if method == NULL_METHOD:
            method = None
        tag_set = set(tags.get(tid, ()))
        is_transfer = row["transfer_peer"] is not None or row["transfer_account"] is not None
        if parent is not None:
            tag_set |= tags.get(parent["_id"], set())
            is_transfer = is_transfer or (
                parent["transfer_peer"] is not None or parent["transfer_account"] is not None
            )

        date = datetime.fromtimestamp(row["date"], zone).date().isoformat()
        payee = payees.get(payee_id)
        uuid = row["uuid"]
        if not uuid:
            stats.missing_uuid += 1
            key = (label, date, row["amount"], " > ".join(path), payee, comment)
            uuid = _synthetic_uuid(key, occurrences[key])
            occurrences[key] += 1

        orig_cur = row["original_currency"] or None
        orig_amt = row["original_amount"]
        if orig_cur is None or orig_amt is None:
            orig_cur, orig_val = None, None
        else:
            orig_val = _scaled(orig_amt, digits.get(orig_cur, DEFAULT_FRACTION_DIGITS))

        records.append(
            SourceRecord(
                uuid=uuid,
                date=date,
                account=label,
                account_currency=acc_currency,
                amount=_scaled(row["amount"], digits.get(acc_currency, DEFAULT_FRACTION_DIGITS)),
                original_currency=orig_cur,
                original_amount=orig_val,
                category_path=" > ".join(path) if path else None,
                category_top=path[0] if path else None,
                category_sub=path[1] if len(path) > 1 else None,
                payee=payee,
                comment=comment,
                method=method,
                tags=sorted(tag_set),
                is_transfer=is_transfer,
            )
        )

    stats.read = len(records)
    stats.skipped_by_status = dict(sorted(skipped.items()))
    return SourceData(records=records, stats=stats)
