"""Rebuild the derived `expenses` table, indexes, FTS and views (DESIGN.md section 7)."""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

import sqlite_utils

from .fx import RateLookup, to_eur
from .rules import Actions, Rules, split_category_path

COLUMNS = (
    ("uuid", "TEXT PRIMARY KEY"),
    ("date", "TEXT NOT NULL"),
    ("year", "INTEGER"),
    ("month", "TEXT"),
    ("account", "TEXT"),
    ("account_currency", "TEXT"),
    ("amount", "REAL"),
    ("original_currency", "TEXT"),
    ("original_amount", "REAL"),
    ("amount_eur", "REAL"),
    ("spend_eur", "REAL"),
    ("kind", "TEXT NOT NULL"),
    ("category_path", "TEXT"),
    ("category_top", "TEXT"),
    ("category_sub", "TEXT"),
    ("payee", "TEXT"),
    ("comment", "TEXT"),
    ("tags", "TEXT"),
    ("is_work", "INTEGER NOT NULL"),
    ("in_country", "INTEGER NOT NULL"),
    ("trip", "TEXT"),
    ("one_off", "INTEGER NOT NULL"),
    ("outlier", "INTEGER NOT NULL"),
    ("rule_notes", "TEXT"),
)
FTS_COLUMNS = ["payee", "comment", "category_path"]
MAD_SCALE = 0.6745
MEAN_AD_SCALE = 1.2533  # mean absolute deviation -> sigma, for when the MAD is zero

VIEWS = {
    # income rows (refunds) are included: spend_eur is negative for them, so they net out
    "spending": "kind IN ('expense', 'income')",
    "spending_recurring": "kind IN ('expense', 'income') AND one_off = 0",
    "transfers": "kind = 'transfer'",
    "uncategorized": "kind != 'transfer' AND category_path IS NULL",
    "flagged": "one_off = 1 OR outlier = 1",
}
INDEXES = ("date", "month", "category_top", "kind")


@dataclass
class BuildResult:
    rows: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    missing_eur: int = 0
    one_offs: int = 0
    outliers: int = 0
    rules_applied: dict[str, int] = field(default_factory=dict)  # counts per rule step


def _set_category(row: dict[str, Any], category: str) -> None:
    if category.strip():
        row["category_path"] = category
        row["category_top"], row["category_sub"] = split_category_path(category)
    else:
        row["category_path"] = row["category_top"] = row["category_sub"] = None


def _apply_actions(row: dict[str, Any], act: Actions) -> None:
    if act.category is not None:
        _set_category(row, act.category)
    if act.payee is not None:
        row["payee"] = act.payee
    if act.comment is not None:
        row["comment"] = act.comment
    removed = {t.casefold() for t in act.remove_tags}
    tags = [t for t in row["tags"] if t.casefold() not in removed]
    for t in act.add_tags:
        if t.casefold() not in {x.casefold() for x in tags}:
            tags.append(t)
    row["tags"] = tags


def _is_one_off_category(row: dict[str, Any], categories: set[str]) -> bool:
    path, top = row["category_path"], row["category_top"]
    if path is None:
        return False
    return bool(categories & {path.casefold(), (top or "").casefold()})


def _outlier_ids(rows: list[dict[str, Any]], rules: Rules) -> set[str]:
    cfg = rules.outliers
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        spend = row["spend_eur"]
        if row["kind"] == "expense" and spend is not None and spend > 0 and row["category_path"]:
            groups[row["category_path"]].append(row)
    flagged: set[str] = set()
    for group in groups.values():
        if len(group) < cfg.min_samples:
            continue
        # Spending amounts are right-skewed, so measure the spread on a log scale.
        values = [math.log(r["spend_eur"]) for r in group]
        median = statistics.median(values)
        mad = statistics.median(abs(v - median) for v in values)
        if mad > 0:
            scale = mad / MAD_SCALE
        else:  # more than half the values are identical: use the mean absolute deviation
            mean_ad = statistics.fmean(abs(v - median) for v in values)
            if mean_ad == 0:
                continue
            scale = mean_ad * MEAN_AD_SCALE
        flagged.update(
            r["uuid"] for r, v in zip(group, values) if (v - median) / scale > cfg.threshold
        )
    return flagged


def _derive(
    conn: sqlite3.Connection, rules: Rules, lookup: RateLookup, counts: Counter[str]
) -> list[dict[str, Any]]:
    cur = conn.execute("SELECT * FROM transactions WHERE deleted_at IS NULL ORDER BY date, uuid")
    names = [d[0] for d in cur.description]
    aliases = {k.casefold(): v for k, v in rules.account_aliases.items()}
    overrides = {o.uuid: o.set for o in rules.overrides}
    seen_overrides: set[str] = set()
    one_off_cats = {c.casefold() for c in rules.one_off.categories}
    dims = rules.dimensions

    rows: list[dict[str, Any]] = []
    forced_one_off: dict[str, bool] = {}
    forced_outlier: dict[str, bool] = {}
    for values in cur.fetchall():
        src = dict(zip(names, values, strict=True))
        row: dict[str, Any] = {k: src[k] for k, _ in COLUMNS if k in src}
        row["tags"] = json.loads(src["tags"] or "[]")
        notes: list[str] = []

        target = aliases.get(row["account"].casefold())
        if target is not None and target != row["account"]:
            row["account"] = target
            notes.append("account_alias")
            counts["account_aliases"] += 1

        for i, rule in enumerate(rules.payee_rules):
            if rule.match.matches(row):
                _apply_actions(row, rule.set)
                notes.append(f"payee_rule#{i}")
                counts["payee_rules"] += 1
                break

        is_transfer = bool(src["is_transfer"])
        for i, rule in enumerate(rules.transfer_rules):
            if rule.match.matches(row):
                if not is_transfer:
                    notes.append(f"transfer_rule#{i}")
                    counts["transfer_rules"] += 1
                is_transfer = True
                break

        kind = "transfer" if is_transfer else ("expense" if row["amount"] < 0 else "income")
        override = overrides.get(row["uuid"])
        if override is not None:
            seen_overrides.add(row["uuid"])
            _apply_actions(row, override)
            kind = override.kind or kind
            if override.one_off is not None:
                forced_one_off[row["uuid"]] = override.one_off
            if override.outlier is not None:
                forced_outlier[row["uuid"]] = override.outlier
            notes.append("override")
            counts["overrides"] += 1

        amount_eur = to_eur(
            row["amount"], row["account_currency"], row["original_amount"],
            row["original_currency"], row["date"], lookup,
        )
        tags = row["tags"]
        row.update(
            kind=kind,
            year=int(row["date"][:4]),
            month=row["date"][:7],
            amount_eur=amount_eur,
            spend_eur=None if amount_eur is None else round(-amount_eur, 2),
            is_work=int(dims.is_work(tags)),
            in_country=int(dims.in_country(tags)),
            trip=dims.trip_for(tags),
            outlier=0,
            rule_notes=",".join(notes) or None,
        )
        spend = row["spend_eur"]
        row["one_off"] = int(
            kind == "expense"
            and (
                _is_one_off_category(row, one_off_cats)
                or (
                    row["category_path"] is None
                    and spend is not None
                    and spend >= rules.one_off.uncategorized_threshold_eur
                )
            )
        )
        rows.append(row)

    counts["overrides_unmatched"] = len(overrides) - len(seen_overrides)
    flagged = _outlier_ids(rows, rules)
    for row in rows:
        row["outlier"] = int(forced_outlier.get(row["uuid"], row["uuid"] in flagged))
        row["one_off"] = int(forced_one_off.get(row["uuid"], bool(row["one_off"])))
        row["tags"] = json.dumps(row["tags"], ensure_ascii=False)
    return rows


def _create_extras(conn: sqlite3.Connection) -> None:
    for col in INDEXES:
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_expenses_{col} ON expenses ({col})")
    sqlite_utils.Database(conn)["expenses"].enable_fts(
        FTS_COLUMNS, create_triggers=False, replace=True
    )
    for name, where in VIEWS.items():
        conn.execute(f"CREATE VIEW {name} AS SELECT * FROM expenses WHERE {where}")
    conn.commit()


def build(conn: sqlite3.Connection, rules: Rules, lookup: RateLookup) -> BuildResult:
    """Rebuild `expenses` (plus indexes, FTS table and views) from live transactions."""
    counts: Counter[str] = Counter()
    rows = _derive(conn, rules, lookup, counts)

    cols = [name for name, _ in COLUMNS]
    ddl = ", ".join(f"{name} {typ}" for name, typ in COLUMNS)
    try:
        conn.execute("BEGIN")
        for view in VIEWS:
            conn.execute(f"DROP VIEW IF EXISTS {view}")
        conn.execute("DROP TABLE IF EXISTS expenses_fts")
        conn.execute("DROP TABLE IF EXISTS expenses")
        conn.execute(f"CREATE TABLE expenses ({ddl})")
        conn.executemany(
            f"INSERT INTO expenses ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})",
            rows,
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    _create_extras(conn)

    return BuildResult(
        rows=len(rows),
        by_kind=dict(Counter(r["kind"] for r in rows)),
        missing_eur=sum(r["amount_eur"] is None for r in rows),
        one_offs=sum(r["one_off"] for r in rows),
        outliers=sum(r["outlier"] for r in rows),
        rules_applied=dict(counts),
    )
