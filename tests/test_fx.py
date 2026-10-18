import io
import sqlite3
import urllib.error
from datetime import date

import pytest

from expenses import fx
from expenses.fx import FxError, RateLookup, ensure_schema, to_eur, update_rates

TODAY = date(2026, 3, 10)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    ensure_schema(c)
    return c


class FakeFetcher:
    """Returns a weekday-only rate for each requested currency/day; records calls."""

    def __init__(self, unsupported=(), fail=False):
        self.calls: list[tuple[list[str], date, date]] = []
        self.unsupported = set(unsupported)
        self.fail = fail

    def __call__(self, currencies, start, end):
        self.calls.append((list(currencies), start, end))
        if self.fail:
            raise FxError("boom")
        rows = []
        d = start
        while d <= end:
            if d.weekday() < 5:
                for c in currencies:
                    if c not in self.unsupported:
                        rows.append((d.isoformat(), c, 2.0))
            d = date.fromordinal(d.toordinal() + 1)
        return rows


def count(conn, cur):
    return conn.execute("SELECT COUNT(*) FROM fx_rates WHERE currency=?", (cur,)).fetchone()[0]


def test_initial_fetch_batches_currencies_and_skips_eur(conn):
    f = FakeFetcher()
    res = update_rates(conn, ["USD", "GBP", "EUR"], date(2026, 3, 2), date(2026, 3, 6), f, TODAY)
    assert f.calls == [(["GBP", "USD"], date(2026, 2, 23), date(2026, 3, 6))]
    assert res.fetched_rows == 2 * 10  # 10 weekdays
    assert res.errors == [] and res.unsupported == []


def test_incremental_gaps_before_and_after(conn):
    f = FakeFetcher()
    update_rates(conn, ["USD"], date(2026, 3, 2), date(2026, 3, 6), f, TODAY)
    f.calls.clear()
    update_rates(conn, ["USD"], date(2026, 3, 2), date(2026, 3, 6), f, TODAY)
    assert f.calls == []  # fully cached

    # extend backwards and forwards (new "today" so the same-day skip doesn't apply)
    later = date(2026, 3, 11)
    update_rates(conn, ["USD"], date(2026, 2, 16), date(2026, 3, 11), f, later)
    assert f.calls == [
        (["USD"], date(2026, 2, 9), date(2026, 2, 22)),
        (["USD"], date(2026, 3, 7), date(2026, 3, 11)),
    ]


def test_end_is_capped_at_today(conn):
    f = FakeFetcher()
    update_rates(conn, ["USD"], date(2026, 3, 2), date(2030, 1, 1), f, TODAY)
    assert f.calls[0][2] == TODAY


def test_after_gap_checked_only_once_per_day(conn):
    f = FakeFetcher()
    update_rates(conn, ["USD"], date(2026, 3, 2), date(2026, 3, 6), f, TODAY)
    f.calls.clear()
    update_rates(conn, ["USD"], date(2026, 3, 2), date(2026, 3, 10), f, TODAY)
    assert f.calls == []  # already checked today
    update_rates(conn, ["USD"], date(2026, 3, 2), date(2026, 3, 10), f, date(2026, 3, 11))
    assert f.calls == [(["USD"], date(2026, 3, 7), date(2026, 3, 10))]


def test_error_is_recorded_not_raised(conn):
    res = update_rates(
        conn, ["USD"], date(2026, 3, 2), date(2026, 3, 6), FakeFetcher(fail=True), TODAY
    )
    assert res.fetched_rows == 0
    assert len(res.errors) == 1 and "boom" in res.errors[0]
    assert res.unsupported == []  # a failure is not "unsupported"
    # failure is retried next time
    f = FakeFetcher()
    update_rates(conn, ["USD"], date(2026, 3, 2), date(2026, 3, 6), f, TODAY)
    assert len(f.calls) == 1


def test_unsupported_currency(conn):
    f = FakeFetcher(unsupported=["UZS"])
    res = update_rates(conn, ["USD", "UZS"], date(2026, 3, 2), date(2026, 3, 6), f, TODAY)
    assert res.unsupported == ["UZS"]
    assert count(conn, "USD") > 0 and count(conn, "UZS") == 0
    # reported again without refetching the same day
    f.calls.clear()
    res = update_rates(conn, ["UZS"], date(2026, 3, 2), date(2026, 3, 6), f, TODAY)
    assert res.unsupported == ["UZS"] and f.calls == []


def make_lookup(conn, rows):
    conn.executemany("INSERT INTO fx_rates VALUES (?, ?, ?)", rows)
    return RateLookup(conn)


def test_lookup_fallbacks(conn):
    lk = make_lookup(conn, [("2026-03-06", "USD", 1.1), ("2026-03-09", "USD", 1.2)])
    assert lk.rate("EUR", "2026-03-07") == 1.0
    assert lk.rate("USD", "2026-03-06") == 1.1  # exact
    assert lk.rate("USD", "2026-03-08") == 1.1  # weekend -> earlier
    assert lk.rate("USD", "2026-03-15") == 1.2  # after last -> latest
    assert lk.rate("USD", "2026-03-02") == 1.1  # before first -> earliest later
    assert lk.rate("XXX", "2026-03-06") is None


def test_lookup_gap_limit(conn):
    lk = make_lookup(conn, [("2026-03-06", "USD", 1.1)])
    gap = fx.MAX_RATE_GAP_DAYS
    assert lk.rate("USD", "2026-03-16") == 1.1 and gap == 10  # exactly 10 days away
    assert lk.rate("USD", "2026-03-17") is None  # 11 days after the last rate
    assert lk.rate("USD", "2026-02-24") == 1.1  # 10 days before the first
    assert lk.rate("USD", "2026-02-23") is None
    assert to_eur(-10.0, "USD", None, None, "2026-06-01", lk) is None


def test_head_range_not_refetched_when_ecb_has_no_earlier_data(conn):
    class LateData(FakeFetcher):
        def __call__(self, currencies, start, end):
            rows = super().__call__(currencies, max(start, date(2026, 3, 2)), end)
            self.calls[-1] = (list(currencies), start, end)  # record what was asked
            return rows

    f = LateData()
    update_rates(conn, ["USD"], date(2026, 2, 1), date(2026, 3, 6), f, TODAY)
    assert len(f.calls) == 1 and count(conn, "USD") > 0
    f.calls.clear()
    later = date(2026, 3, 11)
    update_rates(conn, ["USD"], date(2026, 2, 1), date(2026, 3, 11), f, later)
    # only the tail is fetched; the head (before the first cached rate) is not asked again
    assert f.calls == [(["USD"], date(2026, 3, 7), date(2026, 3, 11))]
    # an even earlier start is still requested once
    f.calls.clear()
    update_rates(conn, ["USD"], date(2026, 1, 1), date(2026, 3, 11), f, later)
    assert [c[1:] for c in f.calls] == [(date(2025, 12, 25), date(2026, 3, 1))]


def test_old_fx_meta_table_is_migrated():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE fx_meta (currency TEXT PRIMARY KEY, last_checked_date TEXT NOT NULL)")
    ensure_schema(c)
    assert "earliest_start" in {r[1] for r in c.execute("PRAGMA table_info(fx_meta)")}


def test_to_eur_branches(conn):
    lk = make_lookup(conn, [("2026-03-06", "USD", 2.0)])
    assert to_eur(10.0, "EUR", 99.0, "USD", "2026-03-06", lk) == 10.0
    assert to_eur(5000.0, "UZS", 12.345, "EUR", "2026-03-06", lk) == 12.35
    assert to_eur(-5000.0, "UZS", 12.345, "EUR", "2026-03-06", lk) == -12.35  # sign of amount
    assert to_eur(5000.0, "UZS", -12.345, "EUR", "2026-03-06", lk) == 12.35
    assert to_eur(-10.0, "USD", None, None, "2026-03-06", lk) == -5.0
    assert to_eur(-10.0, "USD", 3.0, "GBP", "2026-03-06", lk) == -5.0
    assert to_eur(-10.0, "UZS", None, None, "2026-03-06", lk) is None


CSV_SAMPLE = (
    "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE,OBS_STATUS\n"
    "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-03-05,1.0812,A\n"
    "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-03-06,,A\n"
    "EXR.D.GBP.EUR.SP00.A,D,GBP,EUR,SP00,A,2026-03-05,0.8512,A\n"
)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_fetch_ecb_parses_csv(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["ua"] = req.get_header("User-agent")
        seen["timeout"] = timeout
        return FakeResponse(CSV_SAMPLE.encode())

    monkeypatch.setattr(fx.urllib.request, "urlopen", fake_urlopen)
    rows = fx.fetch_ecb(["USD", "GBP"], date(2026, 3, 1), date(2026, 3, 6))
    assert rows == [("2026-03-05", "USD", 1.0812), ("2026-03-05", "GBP", 0.8512)]
    assert seen["url"] == (
        "https://data-api.ecb.europa.eu/service/data/EXR/D.USD+GBP.EUR.SP00.A"
        "?startPeriod=2026-03-01&endPeriod=2026-03-06&format=csvdata"
    )
    assert seen["ua"] and seen["timeout"] == 30


@pytest.mark.parametrize(
    "exc, expect_error",
    [
        (urllib.error.URLError("no route"), True),
        (urllib.error.HTTPError("u", 403, "Forbidden", {}, None), True),
        (urllib.error.HTTPError("u", 404, "Not Found", {}, None), False),
    ],
)
def test_fetch_ecb_errors(monkeypatch, exc, expect_error):
    def fake_urlopen(req, timeout=None):
        raise exc

    monkeypatch.setattr(fx.urllib.request, "urlopen", fake_urlopen)
    if expect_error:
        with pytest.raises(FxError):
            fx.fetch_ecb(["USD"], date(2026, 3, 1), date(2026, 3, 6))
    else:
        assert fx.fetch_ecb(["USD"], date(2026, 3, 1), date(2026, 3, 6)) == []


def test_fetch_ecb_wraps_unexpected_errors(monkeypatch):
    def boom(req, timeout=None):
        raise ValueError("secret detail")

    monkeypatch.setattr(fx.urllib.request, "urlopen", boom)
    with pytest.raises(FxError) as ei:
        fx.fetch_ecb(["USD"], date(2026, 3, 1), date(2026, 3, 6))
    assert "secret detail" not in str(ei.value)

    monkeypatch.setattr(fx.urllib.request, "urlopen", lambda req, timeout=None: FakeResponse(b"x"))
    monkeypatch.setattr(fx, "parse_ecb_csv", lambda text: 1 / 0)
    with pytest.raises(FxError):
        fx.fetch_ecb(["USD"], date(2026, 3, 1), date(2026, 3, 6))
