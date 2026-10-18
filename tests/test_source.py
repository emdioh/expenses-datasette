import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from expenses.source import read_source
from synthetic import SPLIT_LABEL, SyntheticDB


@pytest.fixture
def db(tmp_path: Path) -> SyntheticDB:
    d = SyntheticDB(tmp_path / "BACKUP")
    d.add_account("Main", "EUR")
    d.add_account("Swiss", "CHF")
    d.add_account("Yen", "JPY")
    return d


def by_comment(data, comment):
    return next(r for r in data.records if r.comment == comment)


def test_basic_record_and_minor_units(db):
    db.add_transaction(
        "Main", date(2024, 3, 5), -12.34, category="Food > Groceries", payee="Shop A",
        comment="c1", tags=("TagB", "TagA"), method="Card", uuid="u1",
    )
    db.close()
    data = read_source(db.path)
    (r,) = data.records
    assert r.uuid == "u1"
    assert r.date == "2024-03-05"
    assert (r.account, r.account_currency) == ("Main", "EUR")
    assert r.amount == pytest.approx(-12.34)
    assert r.category_path == "Food > Groceries"
    assert (r.category_top, r.category_sub) == ("Food", "Groceries")
    assert r.payee == "Shop A" and r.comment == "c1" and r.method == "Card"
    assert r.tags == ["TagA", "TagB"]
    assert r.is_transfer is False
    assert r.original_currency is None and r.original_amount is None
    assert data.stats.read == 1


def test_category_levels(db):
    db.add_transaction("Main", date(2024, 1, 1), -1, category="Top")
    db.add_transaction("Main", date(2024, 1, 2), -1, category="A > B > C")
    db.add_transaction("Main", date(2024, 1, 3), -1)
    db.close()
    r1, r2, r3 = read_source(db.path).records
    assert (r1.category_path, r1.category_top, r1.category_sub) == ("Top", "Top", None)
    assert (r2.category_path, r2.category_top, r2.category_sub) == ("A > B > C", "A", "B")
    assert (r3.category_path, r3.category_top, r3.category_sub) == (None, None, None)


def test_dst_dates_read_back_as_local_date(db):
    db.add_transaction("Main", date(2024, 7, 15), -1, comment="summer")
    db.add_transaction("Main", date(2024, 1, 15), -1, comment="winter")
    db.add_transaction("Main", datetime(2024, 3, 31, 23, 30), -1, comment="late")
    db.close()
    data = read_source(db.path)
    assert by_comment(data, "summer").date == "2024-07-15"
    assert by_comment(data, "winter").date == "2024-01-15"
    assert by_comment(data, "late").date == "2024-03-31"
    # Stored as UTC previous day evening
    c = sqlite3.connect(db.path)
    stamps = dict(c.execute("SELECT comment, date FROM transactions"))
    assert datetime.fromtimestamp(stamps["summer"], UTC).hour == 22
    assert datetime.fromtimestamp(stamps["winter"], UTC).hour == 23


def test_other_timezone(db):
    db.add_transaction("Main", date(2024, 7, 15), -1, comment="x")
    db.close()
    # local Rome midnight is 22:00 UTC the day before; UTC reading gives the 14th
    assert read_source(db.path, tz="UTC").records[0].date == "2024-07-14"


def test_jpy_fraction_digits_and_original(db):
    db.add_transaction("Yen", date(2024, 5, 1), -1500, original=(-10.5, "EUR"), comment="y")
    db.add_transaction("Main", date(2024, 5, 2), -10, original=(-1500, "JPY"), comment="e")
    db.close()
    data = read_source(db.path)
    y, e = by_comment(data, "y"), by_comment(data, "e")
    assert y.amount == -1500 and y.account_currency == "JPY"
    assert (y.original_currency, y.original_amount) == ("EUR", pytest.approx(-10.5))
    assert (e.original_currency, e.original_amount) == ("JPY", -1500)


def test_unknown_currency_defaults_to_two_digits(db):
    db.conn.execute("INSERT INTO accounts (label, currency) VALUES ('Odd', 'XXX')")
    db.conn.execute(
        "INSERT INTO transactions (date, value_date, amount, account_id, uuid)"
        " VALUES (0, 0, 250, last_insert_rowid(), 'odd')"
    )
    db.close()
    assert read_source(db.path).records[0].amount == 2.5


def test_original_currency_without_amount_is_dropped(db):
    t = db.add_transaction("Main", date(2024, 5, 1), -5)
    db.conn.execute("UPDATE transactions SET original_currency='USD' WHERE _id=?", (t,))
    db.close()
    (r,) = read_source(db.path).records
    assert r.original_currency is None and r.original_amount is None


def test_empty_comment_and_null_method(db):
    db.add_transaction("Main", date(2024, 5, 1), -5, comment="", method="__NULL__")
    db.close()
    (r,) = read_source(db.path).records
    assert r.comment is None and r.method is None


def test_transfers(db):
    db.add_transaction("Main", date(2024, 2, 1), -100, transfer_to="Swiss", comment="t")
    shared = db.uuid_of(1)
    assert shared == db.uuid_of(2)  # the app gives both legs the same uuid
    acc = dict(db.conn.execute("SELECT label, uuid FROM accounts"))
    db.close()
    data = read_source(db.path)
    assert len(data.records) == 2
    assert all(r.is_transfer for r in data.records)
    main = next(r for r in data.records if r.account == "Main")
    swiss = next(r for r in data.records if r.account == "Swiss")
    assert main.amount == -100 and swiss.amount == 100
    assert main.uuid != swiss.uuid
    assert main.uuid == f"{shared}:{acc['Main']}"
    assert swiss.uuid == f"{shared}:{acc['Swiss']}"


def test_transfer_key_falls_back_to_account_id(db):
    db.add_transaction("Main", date(2024, 2, 1), -10, uuid="x", transfer_to="Swiss")
    db.conn.execute("UPDATE accounts SET uuid = NULL WHERE label = 'Main'")
    db.close()
    keys = {r.account: r.uuid for r in read_source(db.path).records}
    assert keys["Main"] == "x:1"  # Main is account _id 1"


def test_non_transfer_key_is_plain_uuid(db):
    db.add_transaction("Main", date(2024, 2, 1), -10, uuid="plain")
    db.close()
    assert [r.uuid for r in read_source(db.path).records] == ["plain"]


def test_repeated_keys_are_disambiguated(db):
    # same account + uuid is only possible with a different status (unique index)
    db.add_transaction("Main", date(2024, 2, 1), -10, uuid="dup", status=0)
    db.add_transaction("Main", date(2024, 2, 2), -20, uuid="dup", status=1)
    db.add_transaction("Main", date(2024, 2, 3), -30, uuid="dup", status=5)
    db.close()
    data = read_source(db.path)
    keys = [r.uuid for r in data.records]
    assert keys == ["dup", "dup#2", "dup#3"]
    assert data.stats.duplicate_keys == 2
    assert [r.uuid for r in read_source(db.path).records] == keys


def test_split_children_inherit_and_parent_not_emitted(db):
    db.add_split(
        "Main", date(2024, 4, 10),
        [
            {"amount": -6, "category": "Food"},
            {"amount": -4, "category": None, "tags": ("TagB",), "comment": "own"},
        ],
        payee="Shop A", comment="parent note", tags=("TagA",),
    )
    db.close()
    data = read_source(db.path)
    assert len(data.records) == 2
    assert data.stats.splits_expanded == 1
    assert all(r.payee == "Shop A" and r.date == "2024-04-10" for r in data.records)
    assert not any(SPLIT_LABEL in (r.category_path or "") for r in data.records)
    a = next(r for r in data.records if r.amount == -6)
    b = next(r for r in data.records if r.amount == -4)
    assert a.category_path == "Food" and a.comment == "parent note" and a.tags == ["TagA"]
    assert b.category_path is None and b.comment == "own" and b.tags == ["TagA", "TagB"]


def test_archive_parent_skipped_children_kept(db):
    db.add_archive(
        "Main", date(2023, 12, 1),
        [{"amount": -3, "category": "Food"}, {"amount": -7, "category": "Home"}],
        payee="Shop A", tags=("TagA",),
    )
    db.close()
    data = read_source(db.path)
    assert len(data.records) == 2
    assert data.stats.skipped_by_status == {4: 1}
    assert data.stats.splits_expanded == 0
    assert {r.payee for r in data.records} == {"Shop A"}
    assert all(r.tags == ["TagA"] for r in data.records)


def test_status_filtering(db):
    for s in (0, 1, 2, 3, 5):
        db.add_transaction("Main", date(2024, 6, 1), -1, status=s, comment=f"s{s}")
    db.close()
    data = read_source(db.path)
    assert sorted(r.comment for r in data.records) == ["s0", "s1", "s5"]
    assert data.stats.skipped_by_status == {2: 1, 3: 1}
    assert data.stats.read == 3


def test_missing_uuid_is_deterministic(db):
    db.add_transaction("Main", date(2024, 6, 1), -1, uuid=None, payee="Shop A")
    db.add_transaction("Main", date(2024, 6, 1), -1, uuid=None, payee="Shop A")
    db.add_transaction("Main", date(2024, 6, 2), -1, uuid="real")
    db.close()
    data = read_source(db.path)
    uuids = [r.uuid for r in data.records]
    assert len(set(uuids)) == 3
    assert data.stats.missing_uuid == 2
    assert [r.uuid for r in read_source(db.path).records] == uuids


def test_min_max_date(db):
    db.add_transaction("Main", date(2024, 6, 1), -1)
    db.add_transaction("Main", date(2022, 2, 3), -1)
    db.add_transaction("Main", date(2025, 1, 1), -1, status=2)  # skipped
    db.close()
    data = read_source(db.path)
    assert (data.min_date, data.max_date) == ("2022-02-03", "2024-06-01")


def test_empty_db(db):
    db.close()
    data = read_source(db.path)
    assert data.records == [] and data.min_date is None and data.max_date is None


def test_read_only(db):
    db.add_transaction("Main", date(2024, 6, 1), -1)
    db.close()
    before = db.path.read_bytes()
    read_source(db.path)
    assert db.path.read_bytes() == before
