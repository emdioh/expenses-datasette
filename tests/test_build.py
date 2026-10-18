import json
from datetime import date, datetime
from pathlib import Path

import pytest

from expenses import store
from expenses.build import COLUMNS, build
from expenses.fx import RateLookup
from expenses.rules import parse_rules
from expenses.source import read_source
from synthetic import SyntheticDB

EXPECTED_COLUMNS = [
    "uuid", "date", "year", "month", "account", "account_currency", "amount",
    "original_currency", "original_amount", "amount_eur", "spend_eur", "kind",
    "category_path", "category_top", "category_sub", "payee", "comment", "tags", "is_work",
    "in_country", "trip", "one_off", "outlier", "rule_notes",
]


@pytest.fixture
def db(tmp_path: Path) -> SyntheticDB:
    d = SyntheticDB(tmp_path / "BACKUP")
    d.add_account("Main", "EUR")
    d.add_account("Swiss", "CHF")
    d.add_account("Savings", "EUR")
    d.add_account("Misc", "EUR")
    return d


@pytest.fixture
def conn(tmp_path: Path):
    c = store.open_db(tmp_path / "out.db")
    yield c
    c.close()


def run(db, conn, rules_data=None, rates=(("2024-03-01", "CHF", 0.5),)):
    """Import the synthetic backup, insert rates directly and build."""
    db.close()
    store.merge(conn, read_source(db.path), "b1", datetime(2024, 6, 1))
    conn.executemany("INSERT INTO fx_rates VALUES (?,?,?)", rates)
    conn.commit()
    result = build(conn, parse_rules(rules_data or {}), RateLookup(conn))
    return result


def row(conn, uuid):
    return conn.execute("SELECT * FROM expenses WHERE uuid = ?", (uuid,)).fetchone()


def add(db, *args, uuid, **kw):
    db.add_transaction(*args, uuid=uuid, **kw)


def test_columns_match_design(db, conn):
    add(db, "Main", date(2024, 3, 5), -5, uuid="u1")
    run(db, conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(expenses)")]
    assert cols == EXPECTED_COLUMNS == [c for c, _ in COLUMNS]


def test_eur_conversion_branches(db, conn):
    add(db, "Main", date(2024, 3, 5), -10, uuid="eur", original=(-1000, "UZS"))
    add(db, "Swiss", date(2024, 3, 6), -30, uuid="chf_orig_eur", original=(-28, "EUR"))
    add(db, "Swiss", date(2024, 3, 7), -30, uuid="chf_rate")
    add(db, "Swiss", date(2024, 3, 8), -30, uuid="chf_other", original=(-40, "USD"))
    add(db, "Main", date(2024, 3, 9), 20, uuid="refund")
    res = run(db, conn)
    assert row(conn, "eur")["amount_eur"] == -10 and row(conn, "eur")["spend_eur"] == 10
    assert row(conn, "chf_orig_eur")["amount_eur"] == -28
    assert row(conn, "chf_rate")["amount_eur"] == -60
    assert row(conn, "chf_other")["amount_eur"] == -60
    r = row(conn, "refund")
    assert (r["kind"], r["spend_eur"]) == ("income", -20)
    assert res.missing_eur == 0 and res.rows == 5
    assert res.by_kind == {"expense": 4, "income": 1}


def test_missing_rate_gives_null_eur(db, conn):
    add(db, "Swiss", date(2024, 3, 7), -30, uuid="x")
    res = run(db, conn, rates=())
    r = row(conn, "x")
    assert r["amount_eur"] is None and r["spend_eur"] is None
    assert res.missing_eur == 1


def test_split_children_and_dates(db, conn):
    db.add_split(
        "Main", date(2024, 3, 5),
        [{"amount": -10, "category": "Food", "uuid": "s1"},
         {"amount": -5, "category": "Home > Tools", "uuid": "s2"}],
        payee="Shop",
    )
    run(db, conn)
    assert conn.execute("SELECT COUNT(*) FROM expenses").fetchone()[0] == 2
    r = row(conn, "s2")
    assert (r["category_top"], r["category_sub"], r["payee"]) == ("Home", "Tools", "Shop")
    assert (r["year"], r["month"]) == (2024, "2024-03")


def test_transfers_app_and_rule(db, conn):
    add(db, "Main", date(2024, 3, 5), -100, uuid="t1", transfer_to="Savings")
    add(db, "Main", date(2024, 3, 6), -50, uuid="t2", payee="Some Person", category="Family")
    add(db, "Main", date(2024, 3, 7), -50, uuid="t3", payee="Some Person", category="Other")
    res = run(db, conn, {"transfer_rules": [{"match": {"payee": "some person", "category": "Family"}}]})
    t1 = conn.execute("SELECT kind FROM expenses WHERE uuid LIKE 't1:%'").fetchall()
    assert [r["kind"] for r in t1] == ["transfer", "transfer"]  # both legs, keyed per account
    assert row(conn, "t2")["kind"] == "transfer"
    assert row(conn, "t2")["rule_notes"] == "transfer_rule#0"
    assert row(conn, "t3")["kind"] == "expense"
    assert res.by_kind["transfer"] == 3  # incl. the peer row of t1
    assert res.rules_applied["transfer_rules"] == 1
    assert conn.execute("SELECT COUNT(*) FROM transfers").fetchone()[0] == 3


def test_account_alias_keeps_currency(db, conn):
    add(db, "Misc", date(2024, 3, 5), -10, uuid="a1")
    res = run(db, conn, {"account_aliases": {"misc": "Main"}})
    r = row(conn, "a1")
    assert (r["account"], r["account_currency"]) == ("Main", "EUR")
    assert r["rule_notes"] == "account_alias"
    assert res.rules_applied["account_aliases"] == 1


def test_payee_rule_first_match_wins_and_sets_category(db, conn):
    add(db, "Main", date(2024, 3, 5), -10, uuid="p1", payee="Tax Office")
    add(db, "Main", date(2024, 3, 5), -10, uuid="p2", payee="Other", category="Misc")
    rules = {"payee_rules": [
        {"match": {"payee": "tax office"}, "set": {"category": "Tax > Income tax", "add_tags": ["T"]}},
        {"match": {"payee": "regex:tax"}, "set": {"category": "Never"}},
    ]}
    res = run(db, conn, rules)
    r = row(conn, "p1")
    assert (r["category_path"], r["category_top"], r["category_sub"]) == (
        "Tax > Income tax", "Tax", "Income tax")
    assert json.loads(r["tags"]) == ["T"]
    assert r["rule_notes"] == "payee_rule#0"
    assert row(conn, "p2")["rule_notes"] is None
    assert res.rules_applied["payee_rules"] == 1


def test_override_sets_fields_and_notes(db, conn):
    add(db, "Main", date(2024, 3, 5), -10, uuid="o1", payee="X", tags=["A"])
    rules = {"overrides": [
        {"uuid": "o1", "set": {"category": "Gifts", "payee": "Shop", "one_off": True,
                               "add_tags": ["B"], "remove_tags": ["A"], "kind": "income"}},
        {"uuid": "missing", "set": {"one_off": True}},
    ]}
    res = run(db, conn, rules)
    r = row(conn, "o1")
    assert (r["category_path"], r["payee"], r["one_off"], r["kind"]) == ("Gifts", "Shop", 1, "income")
    assert json.loads(r["tags"]) == ["B"]
    assert r["rule_notes"] == "override"
    assert res.rules_applied["overrides"] == 1
    assert res.rules_applied["overrides_unmatched"] == 1


def test_one_off_by_category_and_threshold(db, conn):
    add(db, "Main", date(2024, 3, 1), -50, uuid="cat_top", category="Tax > Income")
    add(db, "Main", date(2024, 3, 2), -50, uuid="cat_path", category="Car > Purchase")
    add(db, "Main", date(2024, 3, 3), -50, uuid="plain", category="Car > Service")
    add(db, "Main", date(2024, 3, 4), -10000, uuid="big_uncat")
    add(db, "Main", date(2024, 3, 5), -9999.99, uuid="almost_uncat")
    add(db, "Main", date(2024, 3, 6), -20000, uuid="big_cat", category="Food")
    add(db, "Main", date(2024, 3, 7), 20000, uuid="big_income")
    rules = {"one_off": {"categories": ["tax", "Car > Purchase"]}}
    res = run(db, conn, rules)
    flags = {u: row(conn, u)["one_off"] for u in
             ("cat_top", "cat_path", "plain", "big_uncat", "almost_uncat", "big_cat", "big_income")}
    assert flags == {"cat_top": 1, "cat_path": 1, "plain": 0, "big_uncat": 1,
                     "almost_uncat": 0, "big_cat": 0, "big_income": 0}
    assert res.one_offs == 3
    # spending_recurring includes the income row (refund) but not the 3 one-offs
    assert conn.execute("SELECT COUNT(*) FROM spending_recurring").fetchone()[0] == 4


def test_uncategorized_after_rule_is_not_threshold_one_off(db, conn):
    add(db, "Main", date(2024, 3, 4), -10000, uuid="big", payee="Landlord")
    run(db, conn, {"payee_rules": [{"match": {"payee": "Landlord"}, "set": {"category": "Home"}}]})
    assert row(conn, "big")["one_off"] == 0


def seed_outliers(db, n=9):
    for i in range(n):
        add(db, "Main", date(2024, 3, 1 + i), -(10 + i), uuid=f"n{i}", category="Food")
    add(db, "Main", date(2024, 3, 20), -500, uuid="spike", category="Food")


def test_outlier_detection(db, conn):
    seed_outliers(db)
    add(db, "Main", date(2024, 3, 21), -500, uuid="lonely", category="Rare")
    res = run(db, conn)
    assert row(conn, "spike")["outlier"] == 1
    assert row(conn, "n0")["outlier"] == 0
    assert row(conn, "lonely")["outlier"] == 0
    assert res.outliers == 1
    assert conn.execute("SELECT COUNT(*) FROM flagged").fetchone()[0] == 1


def test_outlier_small_group_and_zero_mad(db, conn):
    # MAD is 0 (most values identical): falls back to the mean absolute deviation
    for i in range(7):
        add(db, "Main", date(2024, 3, 1 + i), -10, uuid=f"s{i}", category="Small")
    add(db, "Main", date(2024, 3, 10), -900, uuid="small_spike", category="Small")  # 8 rows, MAD 0
    for i in range(3):
        add(db, "Main", date(2024, 3, 1 + i), -10, uuid=f"t{i}", category="Tiny")
    add(db, "Main", date(2024, 3, 10), -900, uuid="tiny_spike", category="Tiny")
    res = run(db, conn)
    assert res.outliers == 1
    assert row(conn, "small_spike")["outlier"] == 1
    assert row(conn, "tiny_spike")["outlier"] == 0  # group below min_samples


def test_outlier_zero_mad_nine_equal_plus_one(db, conn):
    for i in range(9):
        add(db, "Main", date(2024, 3, 1 + i), -800, uuid=f"e{i}", category="Rent")
    add(db, "Main", date(2024, 3, 20), -8000, uuid="big", category="Rent")
    res = run(db, conn)
    assert res.outliers == 1 and row(conn, "big")["outlier"] == 1


def test_outlier_all_identical_no_flags(db, conn):
    for i in range(10):
        add(db, "Main", date(2024, 3, 1 + i), -800, uuid=f"q{i}", category="Rent")
    assert run(db, conn).outliers == 0


def test_dimension_tags_and_tag_actions_case_insensitive(db, conn):
    add(db, "Main", date(2024, 3, 1), -5, uuid="c1", tags=["worktag", "HOME", "lisbon"])
    add(db, "Main", date(2024, 3, 2), -5, uuid="c2", tags=["Keep", "Gone"])
    rules = {
        "dimensions": {"work_tags": ["WorkTag"], "country_tag": "Home",
                       "extra_trip_tags": ["Lisbon"]},
        "overrides": [{"uuid": "c2", "set": {"remove_tags": ["gone"], "add_tags": ["KEEP", "New", "new"]}}],
    }
    run(db, conn, rules)
    c1 = row(conn, "c1")
    assert (c1["is_work"], c1["in_country"], c1["trip"]) == (1, 1, "lisbon")
    assert json.loads(row(conn, "c2")["tags"]) == ["Keep", "New"]


def test_outlier_override_dismiss_and_force(db, conn):
    seed_outliers(db)
    rules = {"overrides": [
        {"uuid": "spike", "set": {"outlier": False}},
        {"uuid": "n3", "set": {"outlier": True}},
    ]}
    run(db, conn, rules)
    assert row(conn, "spike")["outlier"] == 0
    assert row(conn, "n3")["outlier"] == 1


def test_dimensions(db, conn):
    add(db, "Main", date(2024, 3, 1), -5, uuid="d1", tags=["WorkTag", "Home", "Rome24"])
    add(db, "Main", date(2024, 3, 2), -5, uuid="d2", tags=["Lisbon", "Other"])
    add(db, "Main", date(2024, 3, 3), -5, uuid="d3")
    rules = {"dimensions": {"work_tags": ["WorkTag"], "country_tag": "Home",
                            "extra_trip_tags": ["Lisbon"]}}
    run(db, conn, rules)
    d1, d2, d3 = (row(conn, u) for u in ("d1", "d2", "d3"))
    assert (d1["is_work"], d1["in_country"], d1["trip"]) == (1, 1, "Rome24")
    assert (d2["is_work"], d2["in_country"], d2["trip"]) == (0, 0, "Lisbon")
    assert (d3["is_work"], d3["in_country"], d3["trip"]) == (0, 0, None)


def test_views_indexes_and_rebuild(db, conn):
    add(db, "Main", date(2024, 3, 1), -5, uuid="v1", category="Food")
    add(db, "Main", date(2024, 3, 2), -5, uuid="v2")
    add(db, "Main", date(2024, 3, 3), 50, uuid="v3", category="Food")
    add(db, "Main", date(2024, 3, 4), -20, uuid="v4", transfer_to="Savings")
    res = run(db, conn)
    count = lambda v: conn.execute(f"SELECT COUNT(*) FROM {v}").fetchone()[0]  # noqa: E731
    assert count("expenses") == res.rows == 5
    assert (count("spending"), count("spending_recurring")) == (3, 3)  # incl. income (refund)
    net = conn.execute("SELECT SUM(spend_eur) FROM spending WHERE category_path = 'Food'").fetchone()[0]
    assert net == -45  # 5 spent, 50 refunded
    assert count("transfers") == 2
    assert count("uncategorized") == 1  # v2 (transfer rows excluded)
    assert count("flagged") == 0
    idx = {r[1] for r in conn.execute("PRAGMA index_list(expenses)")}
    assert {"idx_expenses_date", "idx_expenses_month", "idx_expenses_category_top",
            "idx_expenses_kind"} <= idx
    # rebuilding from scratch is repeatable
    again = build(conn, parse_rules({}), RateLookup(conn))
    assert again.rows == 5 and count("expenses") == 5


def test_soft_deleted_rows_excluded(db, conn):
    add(db, "Main", date(2024, 3, 1), -5, uuid="keep")
    add(db, "Main", date(2024, 3, 2), -5, uuid="drop")
    run(db, conn)
    conn.execute("UPDATE transactions SET deleted_at = '2024-06-01' WHERE uuid = 'drop'")
    conn.commit()
    res = build(conn, parse_rules({}), RateLookup(conn))
    assert res.rows == 1 and row(conn, "drop") is None


def test_full_text_search(db, conn):
    add(db, "Main", date(2024, 3, 1), -5, uuid="f1", payee="Bakery Rossi", category="Food > Bread")
    add(db, "Main", date(2024, 3, 2), -5, uuid="f2", payee="Garage", comment="winter tyres")
    run(db, conn)
    hits = lambda q: {  # noqa: E731
        r[0] for r in conn.execute(
            "SELECT uuid FROM expenses WHERE rowid IN "
            "(SELECT rowid FROM expenses_fts WHERE expenses_fts MATCH ?)", (q,))
    }
    assert hits("bakery") == {"f1"}
    assert hits("tyres") == {"f2"}
    assert hits("bread") == {"f1"}
    assert hits("nothing") == set()
