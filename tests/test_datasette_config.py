"""The shipped Datasette metadata: valid YAML, all SQL runs, Datasette serves it."""

import asyncio
import re
import shutil
import sqlite3
from pathlib import Path

import pytest
import yaml
from datasette.app import Datasette

from expenses.datasette import config_path, datasette_args, metadata_path, plugins_dir

COLUMNS = """
    uuid text primary key, date text, year integer, month text, account text,
    account_currency text, amount real, original_currency text, original_amount real,
    amount_eur real, spend_eur real, kind text, category_path text, category_top text,
    category_sub text, payee text, comment text, tags text, is_work integer,
    in_country integer, trip text, one_off integer, outlier integer, rule_notes text
"""

# uuid, date, account, ccy, amount, orig_ccy, orig_amount, amount_eur, kind,
# path, top, sub, payee, tags, is_work, in_country, trip, one_off, outlier
ROWS = [
    ("u1", "2024-01-05", "Main", "EUR", -20.0, None, None, -20.0, "expense",
     "Food > Groceries", "Food", "Groceries", "Shop A", '["Home"]', 0, 1, None, 0, 0),
    ("u2", "2024-02-10", "Main", "EUR", -35.5, None, None, -35.5, "expense",
     "Food", "Food", None, "Shop B", "[]", 0, 1, None, 0, 0),
    ("u3", "2024-02-11", "Main", "EUR", 10.0, None, None, 10.0, "income",
     "Food > Groceries", "Food", "Groceries", "Shop A", "[]", 0, 1, None, 0, 0),
    ("u4", "2024-03-01", "Main", "EUR", -900.0, None, None, -900.0, "expense",
     "Travel > Flights", "Travel", "Flights", "Airline", '["Work", "Rome24"]', 1, 0, "Rome24", 1, 1),
    ("u5", "2024-03-02", "Cash", "USD", -50.0, "JPY", -7000.0, None, "expense",
     None, None, None, None, "[]", 0, 0, "Rome24", 0, 0),
    ("u6", "2024-03-03", "Main", "EUR", -100.0, None, None, -100.0, "transfer",
     "Family", "Family", None, "Person", "[]", 0, 1, None, 0, 0),
]

VIEWS = """
    create view spending as select * from expenses where kind in ('expense', 'income');
    create view spending_recurring as
        select * from expenses where kind in ('expense', 'income') and coalesce(one_off, 0) = 0;
    create view transfers as select * from expenses where kind = 'transfer';
    create view uncategorized as
        select * from expenses where kind = 'expense' and category_path is null;
    create view flagged as select * from expenses where one_off = 1 or outlier = 1;
"""

PARAMS = ("from", "to", "include_one_offs", "category")
PARAM_SETS = [
    {},
    {"from": "2024-02", "to": "2024-02-28", "include_one_offs": "0", "category": "Food"},
]


@pytest.fixture(scope="module")
def meta():
    return yaml.safe_load(metadata_path().read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("ds") / "expenses.db"
    con = sqlite3.connect(path)
    con.execute(f"create table expenses ({COLUMNS})")
    con.executemany(f"insert into expenses values ({','.join('?' * 24)})", [
        (u, d, int(d[:4]), d[:7], acc, ccy, amt, oc, oa, eur,
         -eur if eur is not None else None, kind, cp, top, sub, payee, None, tags,
         work, country, trip, oneoff, outlier, None)
        for (u, d, acc, ccy, amt, oc, oa, eur, kind, cp, top, sub, payee, tags,
             work, country, trip, oneoff, outlier) in ROWS
    ])
    con.executescript(VIEWS)
    con.commit()
    con.close()
    return path


def canned(meta):
    return meta["databases"]["expenses"]["queries"]


def dashboards(meta):
    return meta["plugins"]["datasette-dashboards"]


def all_sql(meta):
    for name, query in canned(meta).items():
        yield f"query {name}", query["sql"]
    for slug, dash in dashboards(meta).items():
        for flt_name, flt in dash.get("filters", {}).items():
            if "query" in flt:
                yield f"{slug} filter {flt_name}", flt["query"]
        for chart_slug, chart in dash["charts"].items():
            yield f"{slug}.{chart_slug}", chart["query"]


def run(con, sql, values):
    names = set(re.findall(r":([A-Za-z0-9_]+)", sql))
    return con.execute(sql, {n: values.get(n, "") for n in names}).fetchall()


def test_paths_exist():
    assert metadata_path().is_file() and config_path().is_file()
    args = datasette_args("x.db", 1234)
    assert args[:3] == ["serve", "x.db", "--metadata"] and "1234" in args
    assert args[args.index("--plugins-dir") + 1] == str(plugins_dir())
    assert (plugins_dir() / "host_check.py").is_file()


def test_yaml_structure(meta):
    assert meta["title"] == "Expenses"
    table = meta["databases"]["expenses"]["tables"]["expenses"]
    assert table["facets"] == [
        "kind", "category_top", "account", "year", "is_work", "in_country",
        "trip", "one_off", "outlier",
    ]
    assert table["sort_desc"] == "date"
    assert set(dashboards(meta)) == {"overview", "category"}
    assert dashboards(meta)["overview"]["title"] == "Spending overview"
    for dash in dashboards(meta).values():
        assert set(dash["filters"]) >= {"from", "to", "include_one_offs"}
        areas = {a for row in dash["layout"] for a in row}
        assert areas == set(dash["charts"])
        for chart in dash["charts"].values():
            assert chart["db"] == "expenses"
            assert chart["library"] in {"vega-lite", "metric", "table"}


@pytest.mark.parametrize("values", PARAM_SETS)
def test_all_sql_runs(meta, db_path, values):
    con = sqlite3.connect(db_path)
    try:
        for label, sql in all_sql(meta):
            try:
                run(con, sql, values)
            except sqlite3.Error as exc:
                pytest.fail(f"{label}: {exc}")
    finally:
        con.close()


def test_filters_and_totals(meta, db_path):
    con = sqlite3.connect(db_path)
    try:
        total = lambda slug, values: run(
            con, dashboards(meta)["overview"]["charts"][slug]["query"], values)[0][0]
        # the 10 EUR income row (refund) nets against spending
        assert total("total_spend", {}) == round(20 + 35.5 + 900 - 10)
        assert total("total_spend", {"include_one_offs": "0"}) == round(20 + 35.5 - 10)
        assert total("total_spend", {"from": "2024-02", "to": "2024-02"}) == round(35.5 - 10)
        net = {r[1]: r[2] for r in run(con, canned(meta)["net_by_category"]["sql"], {})}
        assert net["Food > Groceries"] == 10.0  # 20 expense - 10 refund
        tags = {r[0]: r[1] for r in run(con, canned(meta)["spending_by_tag"]["sql"], {})}
        assert tags == {"Home": 20.0, "Work": 900.0, "Rome24": 900.0}
        cat = {r[0]: r[1] for r in run(con, canned(meta)["spending_by_category_path"]["sql"], {})}
        assert cat["Food > Groceries"] == 10.0  # refund netted
    finally:
        con.close()


@pytest.mark.parametrize("flag, included", [
    ("", True), ("1", True), ("yes", True), ("anything", True),
    ("0", False), ("false", False), ("NO", False), (" Off ", False),
])
def test_include_one_offs_parsing(meta, db_path, flag, included):
    con = sqlite3.connect(db_path)
    try:
        sql = dashboards(meta)["overview"]["charts"]["total_spend"]["query"]
        got = run(con, sql, {"include_one_offs": flag})[0][0]
        assert got == (round(945.5) if included else round(45.5))
    finally:
        con.close()


def test_monthly_average_divides_by_month_span(meta, db_path):
    con = sqlite3.connect(db_path)
    try:
        sql = dashboards(meta)["overview"]["charts"]["monthly_avg"]["query"]
        # 2024-01 .. 2024-03 is a span of 3 months (rows of any kind count for the span)
        assert run(con, sql, {})[0][0] == round((20 + 35.5 + 900 - 10) / 3)
        # only 2024-02 in range: 1 month
        assert run(con, sql, {"from": "2024-02", "to": "2024-02"})[0][0] == round(25.5)
        assert run(con, sql, {"from": "2030-01"})[0][0] == 0  # nothing in range
    finally:
        con.close()


def test_original_currency_breakdown_and_missing_eur_range(meta, db_path):
    con = sqlite3.connect(db_path)
    try:
        rows = run(con, canned(meta)["original_currency_breakdown"]["sql"], {})
        by_cur = {(r[0], r[1]): r for r in rows}
        assert by_cur[("EUR", "EUR")][3] == 20 + 35.5 + 900 - 10  # coalesce(original, amount)
        assert by_cur[("JPY", "USD")][3] == 7000
        sql = canned(meta)["missing_eur"]["sql"]
        assert len(run(con, sql, {})) == 1
        assert run(con, sql, {"to": "2024-02"}) == []
        assert len(run(con, sql, {"from": "2024-03", "to": "2024-03"})) == 1
    finally:
        con.close()


def test_table_charts_never_empty_and_escaped(meta, db_path, tmp_path):
    tables = [
        (slug, name, chart["query"])
        for slug, dash in dashboards(meta).items()
        for name, chart in dash["charts"].items() if chart["library"] == "table"
    ]
    assert tables
    con = sqlite3.connect(db_path)
    try:
        for slug, name, sql in tables:  # empty result in range -> one placeholder row
            rows = run(con, sql, {"from": "2030-01"})
            assert len(rows) == 1, (slug, name)
            assert "no flagged rows" in rows[0]
            real = run(con, sql, {})  # real data: placeholder not added
            assert len(real) == 1 and "no flagged rows" not in real[0]
    finally:
        con.close()
    # hostile text is escaped in every text column
    path = tmp_path / "x.db"
    shutil.copy(db_path, path)
    con = sqlite3.connect(path)
    con.execute(
        "update expenses set payee = ?, category_path = ? where uuid = 'u4'",
        ("<img src=x onerror=a&b>\"'", "<script>"),
    )
    con.commit()
    for _, _, sql in tables:
        rows = run(con, sql, {})
        text = " ".join(str(c) for r in rows for c in r if isinstance(c, str))
        for bad in ("<", ">", '"', "'"):
            assert bad not in text
        assert "&lt;img src=x onerror=a&amp;b&gt;&quot;&#39;" in text
        assert "&lt;script&gt;" in text
    con.close()


def test_missing_eur_query(meta, db_path):
    con = sqlite3.connect(db_path)
    rows = run(con, canned(meta)["missing_eur"]["sql"], {})
    con.close()
    assert [r[-1] for r in rows] == ["u5"]


async def _serve(meta, db_path):
    ds = Datasette([str(db_path)], metadata=meta, settings={"max_returned_rows": 5000})
    await ds.invoke_startup()
    results = {}

    async def get(path):
        resp = await ds.client.get(path)
        results[path] = resp
        return resp

    assert (await get("/")).status_code == 200
    assert (await get("/expenses")).status_code == 200
    assert (await get("/expenses/expenses")).status_code == 200
    for name in canned(meta):
        for qs in ("", "?from=2024-02&to=2024-03&include_one_offs=0"):
            resp = await get(f"/expenses/{name}.json{qs}")
            assert resp.status_code == 200, (name, resp.text[:300])
            assert "error" not in resp.json() or not resp.json()["error"]
        assert (await get(f"/expenses/{name}")).status_code == 200
    assert (await get("/-/dashboards")).status_code == 200
    for slug, dash in dashboards(meta).items():
        assert (await get(f"/-/dashboards/{slug}")).status_code == 200
        assert (await get(f"/-/dashboards/{slug}?from=2024-02&include_one_offs=0")).status_code == 200
        # what the dashboard JavaScript fetches for each chart, without filter values
        for chart_slug, chart in dash["charts"].items():
            assert (await get(f"/-/dashboards/{slug}/{chart_slug}")).status_code == 200
            resp = await ds.client.get(
                "/expenses.json", params={"sql": chart["query"], "_shape": "objects"})
            assert resp.status_code == 200, (chart_slug, resp.text[:300])
            assert "rows" in resp.json()
    return results


def test_datasette_serves_config(meta, db_path):
    asyncio.run(_serve(meta, db_path))


def test_category_filter_options(meta, db_path):
    async def go():
        ds = Datasette([str(db_path)], metadata=meta)
        await ds.invoke_startup()
        resp = await ds.client.get("/-/dashboards/category")
        assert resp.status_code == 200
        assert "Food" in resp.text and "Travel" in resp.text

    asyncio.run(go())


@pytest.mark.parametrize("host, status", [
    ("127.0.0.1:8001", 200), ("127.0.0.1", 200), ("localhost:9", 200), ("LOCALHOST", 200),
    ("[::1]:8001", 200),
    ("evil.example", 403), ("evil.example:8001", 403), ("127.0.0.1.evil.example", 403),
    ("localhost.evil.example:8001", 403), ("[::2]:8001", 403), ("", 403),
])
def test_host_check_plugin(meta, db_path, host, status):
    async def go():
        ds = Datasette([str(db_path)], metadata=meta, plugins_dir=str(plugins_dir()))
        await ds.invoke_startup()
        resp = await ds.client.get("/-/versions.json", headers={"Host": host})
        return resp.status_code

    assert asyncio.run(go()) == status
