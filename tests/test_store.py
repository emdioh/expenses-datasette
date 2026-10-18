import json
from datetime import datetime
from pathlib import Path

import pytest

from expenses import store
from expenses.source import SourceData, SourceRecord, SourceStats


def rec(uuid, date="2024-03-10", amount=-10.0, tags=(), **kw) -> SourceRecord:
    base = dict(
        uuid=uuid, date=date, account="Main", account_currency="EUR", amount=amount,
        original_currency=None, original_amount=None, category_path="Food > Shop",
        category_top="Food", category_sub="Shop", payee="P", comment=None, method=None,
        tags=list(tags), is_transfer=False,
    )
    base.update(kw)
    return SourceRecord(**base)


def src(*records) -> SourceData:
    return SourceData(list(records), SourceStats())


@pytest.fixture
def conn(tmp_path: Path):
    c = store.open_db(tmp_path / "sub" / "x.db")
    store.ensure_schema(c)
    yield c
    c.close()


def live(conn):
    return {r["uuid"]: r for r in conn.execute("SELECT * FROM transactions WHERE deleted_at IS NULL")}


def tags_of(conn, uuid):
    return sorted(r[0] for r in conn.execute("SELECT tag FROM transaction_tags WHERE uuid=?", (uuid,)))


def test_open_db_creates_parent_and_schema(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"transactions", "transaction_tags", "imports", "fx_rates"} <= names
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    store.ensure_schema(conn)  # idempotent


def test_insert(conn):
    r = store.merge(conn, src(rec("a", tags=["T1", "T2"]), rec("b")), "b1", None)
    assert (r.inserted, r.updated, r.unchanged) == (2, 0, 0)
    row = live(conn)["a"]
    assert row["first_seen_backup"] == row["last_seen_backup"] == "b1"
    assert json.loads(row["tags"]) == ["T1", "T2"]
    assert tags_of(conn, "a") == ["T1", "T2"]
    imp = conn.execute("SELECT * FROM imports").fetchone()
    assert (imp["backup_file"], imp["inserted"], imp["min_date"]) == ("b1", 2, "2024-03-10")


def test_unchanged_and_idempotent(conn):
    data = src(rec("a"), rec("b"))
    store.merge(conn, data, "b1", datetime(2024, 4, 1))
    r = store.merge(conn, data, "b2", datetime(2024, 4, 2))
    assert (r.inserted, r.updated, r.unchanged) == (0, 0, 2)
    row = live(conn)["a"]
    assert (row["first_seen_backup"], row["last_seen_backup"]) == ("b1", "b2")
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2


def test_update_changes_fields_and_tags(conn):
    store.merge(conn, src(rec("a", tags=["X", "Y"])), "b1")
    r = store.merge(conn, src(rec("a", amount=-12.5, tags=["Y", "Z"], payee="Q")), "b2")
    assert (r.updated, r.unchanged) == (1, 0)
    row = live(conn)["a"]
    assert row["amount"] == -12.5 and row["payee"] == "Q"
    assert (row["first_seen_backup"], row["last_seen_backup"]) == ("b1", "b2")
    assert tags_of(conn, "a") == ["Y", "Z"]


def test_missing_older_than_min_date_is_kept(conn):
    store.merge(conn, src(rec("old", date="2023-01-01"), rec("new")), "b1")
    r = store.merge(conn, src(rec("new")), "b2")
    assert (r.kept_missing, r.dropped) == (1, 0)
    assert "old" in live(conn)


def test_missing_within_range_is_soft_deleted(conn):
    store.merge(conn, src(rec("gone", tags=["T"]), rec("stay", date="2024-03-01")), "b1")
    r = store.merge(conn, src(rec("stay", date="2024-03-01"), rec("x", date="2024-03-20")), "b2")
    assert r.dropped == 1 and r.inserted == 1
    assert "gone" not in live(conn)
    row = conn.execute("SELECT deleted_at FROM transactions WHERE uuid='gone'").fetchone()
    assert row["deleted_at"] is not None
    assert tags_of(conn, "gone") == []
    # a second merge does not count it again
    r2 = store.merge(conn, src(rec("stay", date="2024-03-01"), rec("x", date="2024-03-20")), "b3")
    assert r2.dropped == 0


def test_revive(conn):
    store.merge(conn, src(rec("a", tags=["T"]), rec("b", date="2024-03-01")), "b1")
    store.merge(conn, src(rec("b", date="2024-03-01")), "b2")
    assert "a" not in live(conn)
    r = store.merge(conn, src(rec("a", tags=["T"]), rec("b", date="2024-03-01")), "b3")
    assert r.updated == 1 and r.revived == 1
    row = live(conn)["a"]
    assert row["deleted_at"] is None and row["last_seen_backup"] == "b3"
    assert tags_of(conn, "a") == ["T"]


def test_skip_not_newer_unless_force(conn):
    store.merge(conn, src(rec("a")), "b1", datetime(2024, 5, 1, 12))
    for ts in (datetime(2024, 5, 1, 12), datetime(2024, 4, 1)):
        r = store.merge(conn, src(rec("a"), rec("n")), "b0", ts)
        assert r.skipped and r.skip_reason == "not_newer"
    assert set(live(conn)) == {"a"}
    r = store.merge(conn, src(rec("a"), rec("n")), "b0", datetime(2024, 4, 1), force=True)
    assert not r.skipped and r.inserted == 1
    assert conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 2


def test_empty_source_deletes_nothing(conn):
    store.merge(conn, src(rec("a")), "b1", datetime(2024, 5, 1))
    r = store.merge(conn, src(), "b2", datetime(2024, 6, 1), force=True)
    assert r.skipped and r.empty_source and r.skip_reason == "empty_source"
    assert "a" in live(conn)
    assert conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 1


def test_failure_rolls_back_whole_merge(conn):
    store.merge(conn, src(rec("a")), "b1")
    bad = rec("c")
    bad.account = None  # violates NOT NULL
    with pytest.raises(Exception):
        store.merge(conn, src(rec("n"), bad), "b2")
    assert set(live(conn)) == {"a"}
    assert conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 1


def test_tags_stay_in_sync(conn):
    store.merge(conn, src(rec("a", tags=["A"]), rec("b", tags=["B", "C"])), "b1")
    store.merge(conn, src(rec("a", tags=[]), rec("b", tags=["B", "C"])), "b2")
    assert tags_of(conn, "a") == []
    assert tags_of(conn, "b") == ["B", "C"]
    n = conn.execute("SELECT COUNT(*) FROM transaction_tags").fetchone()[0]
    assert n == 2
