"""ECB daily reference rates, cached in the analysis DB, and EUR conversion."""

from __future__ import annotations

import bisect
import csv
import io
import sqlite3
import urllib.error
import urllib.request
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

ECB_URL = "https://data-api.ecb.europa.eu/service/data/EXR/D.{cur}.EUR.SP00.A"
USER_AGENT = "expenses-datasette/0.1 (+personal expense analysis)"
TIMEOUT_SECONDS = 30
LOOKBACK_DAYS = 7  # extra days before `start` so weekends/holidays resolve

Row = tuple[str, str, float]  # (date iso, currency, rate per 1 EUR)
Fetcher = Callable[[list[str], date, date], list[Row]]


class FxError(Exception):
    """Network or HTTP failure while fetching rates."""


@dataclass
class FxUpdateResult:
    fetched_rows: int = 0
    errors: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS fx_rates ("
        "date TEXT NOT NULL, currency TEXT NOT NULL, rate_per_eur REAL NOT NULL, "
        "PRIMARY KEY (date, currency))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS fx_meta ("
        "currency TEXT PRIMARY KEY, last_checked_date TEXT NOT NULL)"
    )
    conn.commit()


def parse_ecb_csv(text: str) -> list[Row]:
    rows: list[Row] = []
    for rec in csv.DictReader(io.StringIO(text)):
        value = (rec.get("OBS_VALUE") or "").strip()
        day = (rec.get("TIME_PERIOD") or "").strip()
        cur = (rec.get("CURRENCY") or "").strip()
        if not value or not day or not cur:
            continue
        try:
            rows.append((day, cur, float(value)))
        except ValueError:
            continue
    return rows


def fetch_ecb(currencies: list[str], start: date, end: date) -> list[Row]:
    """Fetch daily rates (units of currency per 1 EUR) from the ECB data portal."""
    url = (
        ECB_URL.format(cur="+".join(currencies))
        + f"?startPeriod={start.isoformat()}&endPeriod={end.isoformat()}&format=csvdata"
    )
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            text = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:  # ECB: no data for the series/period
            return []
        raise FxError(f"ECB request failed: HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, UnicodeDecodeError) as exc:
        raise FxError(f"ECB request failed: {exc}") from exc
    return parse_ecb_csv(text)


def _cached_span(conn: sqlite3.Connection, currency: str) -> tuple[date, date] | None:
    lo, hi = conn.execute(
        "SELECT MIN(date), MAX(date) FROM fx_rates WHERE currency = ?", (currency,)
    ).fetchone()
    if lo is None:
        return None
    return date.fromisoformat(lo), date.fromisoformat(hi)


def _last_checked(conn: sqlite3.Connection, currency: str) -> str | None:
    row = conn.execute(
        "SELECT last_checked_date FROM fx_meta WHERE currency = ?", (currency,)
    ).fetchone()
    return row[0] if row else None


def update_rates(
    conn: sqlite3.Connection,
    currencies: list[str],
    start: date,
    end: date,
    fetcher: Fetcher = fetch_ecb,
    today: date | None = None,
) -> FxUpdateResult:
    """Fetch and cache only the rate ranges missing for `currencies` in [start, end]."""
    ensure_schema(conn)
    today = today or date.today()
    result = FxUpdateResult()
    want_from = start - timedelta(days=LOOKBACK_DAYS)
    want_to = min(end, today)
    today_iso = today.isoformat()

    # (range) -> currencies needing it; "after" ranges also update last_checked
    plan: dict[tuple[date, date], list[str]] = defaultdict(list)
    after_ranges: set[tuple[date, date, str]] = set()
    skipped_unsupported: list[str] = []
    for cur in sorted({c.upper() for c in currencies} - {"EUR"}):
        span = _cached_span(conn, cur)
        checked_today = _last_checked(conn, cur) == today_iso
        if span is None:
            if checked_today:
                skipped_unsupported.append(cur)
            elif want_from <= want_to:
                plan[(want_from, want_to)].append(cur)
                after_ranges.add((want_from, want_to, cur))
            continue
        lo, hi = span
        if lo > start:
            plan[(want_from, lo - timedelta(days=1))].append(cur)
        if want_to > hi and not checked_today:
            rng = (hi + timedelta(days=1), want_to)
            plan[rng].append(cur)
            after_ranges.add((*rng, cur))

    result.unsupported.extend(skipped_unsupported)
    got: dict[str, int] = defaultdict(int)
    failed: set[str] = set()
    for (r_start, r_end), curs in sorted(plan.items()):
        try:
            rows = fetcher(curs, r_start, r_end)
        except FxError as exc:
            result.errors.append(f"{','.join(curs)} {r_start}..{r_end}: {exc}")
            failed.update(curs)
            continue
        wanted = set(curs)
        rows = [r for r in rows if r[1] in wanted]
        conn.executemany(
            "INSERT OR REPLACE INTO fx_rates (date, currency, rate_per_eur) VALUES (?, ?, ?)",
            rows,
        )
        result.fetched_rows += len(rows)
        for _, cur, _ in rows:
            got[cur] += 1
        for cur in curs:
            if (r_start, r_end, cur) in after_ranges:
                conn.execute(
                    "INSERT OR REPLACE INTO fx_meta (currency, last_checked_date) VALUES (?, ?)",
                    (cur, today_iso),
                )
    conn.commit()

    for cur in sorted({c for curs in plan.values() for c in curs}):
        if cur not in failed and not got[cur] and _cached_span(conn, cur) is None:
            result.unsupported.append(cur)
    return result


class RateLookup:
    """In-memory rate lookup with weekend/holiday fallbacks."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._dates: dict[str, list[str]] = defaultdict(list)
        self._rates: dict[str, list[float]] = defaultdict(list)
        for day, cur, rate in conn.execute(
            "SELECT date, currency, rate_per_eur FROM fx_rates ORDER BY currency, date"
        ):
            self._dates[cur].append(day)
            self._rates[cur].append(rate)

    def rate(self, currency: str, date_iso: str) -> float | None:
        if currency == "EUR":
            return 1.0
        dates = self._dates.get(currency)
        if not dates:
            return None
        i = bisect.bisect_right(dates, date_iso)  # dates[:i] are <= date_iso
        return self._rates[currency][i - 1 if i > 0 else 0]


def to_eur(
    amount: float,
    account_currency: str,
    original_amount: float | None,
    original_currency: str | None,
    date_iso: str,
    lookup: RateLookup,
) -> float | None:
    """Convert a transaction to EUR (see DESIGN.md section 5); None if no rate."""
    if account_currency == "EUR":
        return round(amount, 2)
    if original_currency == "EUR" and original_amount is not None:
        return round(original_amount, 2)
    rate = lookup.rate(account_currency, date_iso)
    if not rate:
        return None
    return round(amount / rate, 2)
