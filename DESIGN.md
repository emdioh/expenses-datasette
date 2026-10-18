# expenses-datasette — design

Analyse personal expenses recorded with the [MyExpenses](https://github.com/mtotschnig/MyExpenses)
Android app. A single command picks up the newest MyExpenses backup, merges it into a
persistent local analysis database, converts every amount to EUR with ECB reference rates
(keeping the original currency), applies user-defined correction rules and serves the result
with [Datasette](https://datasette.io) plus ready-made dashboards and queries.

## Privacy rules (hard requirements)

- **No personal data in the repository**: no backups, no analysis database, no real rules
  file, no real payee/tag/person names or amounts in code, tests, docs, commit messages or
  issue tracker entries. Tests use synthetic fixtures only.
- The real rules file and config live outside the repo (`~/.config/expenses/`); the repo ships
  `rules.example.yaml` with made-up values.
- The tool must not print transaction details to stdout/stderr or log files by default:
  reports print counts and totals only. A `--verbose` flag may list individual rows.
- Backup passwords are never written to disk by the tool and never logged.

## Overview

```
phone (MyExpenses auto-backup)
  └─ Syncthing ─▶ ~/MyExpensesBackups/backup-YYYYMMDD-HHMMSS.{zip,enc}
                      │  expenses run
                      ▼
  backup.py   newest file → (decrypt) → unzip to temp dir → BACKUP (SQLite, read-only)
  source.py   MyExpenses schema → normalized source records
  store.py    merge into analysis DB (~/.local/share/expenses/expenses.db), import report
  fx.py       ECB daily rates, cached in the analysis DB
  rules.py    load + validate rules.yaml
  build.py    derived `expenses` table: EUR amounts, kind, dimensions, flags, outliers
  serve       datasette expenses.db --metadata ... (dashboards, canned queries)
```

Python ≥ 3.11, managed with `uv`. Package `expenses` in `src/expenses/`, CLI entry point
`expenses`. Dependencies: `datasette`, `datasette-dashboards`, `sqlite-utils`, `pyyaml`,
`cryptography`, `httpx` (or stdlib urllib), `pytest` for tests.

## Configuration

`~/.config/expenses/config.toml` (all keys optional, CLI flags override):

```toml
backup_dir = "~/MyExpensesBackups"
data_dir   = "~/.local/share/expenses"      # expenses.db + archived backups
rules      = "~/.config/expenses/rules.yaml"
timezone   = "Europe/Rome"
# password: from env EXPENSES_BACKUP_PASSWORD, else prompted (getpass) when an .enc is found
```

## 1. Backups (`backup.py`)

- Candidates: files in `backup_dir` matching `backup-YYYYMMDD-HHMMSS.zip|.enc` (the prefix is
  configurable in the app; accept any `<prefix>-YYYYMMDD-HHMMSS.(zip|enc)`). Newest by the
  timestamp in the name, falling back to mtime.
- `.enc` format (from MyExpenses `EncryptionHelper`): `b"ME_ENC_01"` (9 bytes) + 12-byte IV +
  AES-GCM ciphertext of the whole zip with the 16-byte tag appended; key = SHA-256 of the
  UTF-8 password. Wrong password → clear error, no traceback with secrets.
- Zip contents: `BACKUP` (SQLite DB, plaintext even if the app DB is encrypted), `BACKUP_PREF`
  (Android shared prefs XML), optionally `ui_settings.preferences_pb`, attachments. Only
  `BACKUP` is used; `home_currency` may be read from `BACKUP_PREF`. Guard against path
  traversal; extract to a `tempfile.TemporaryDirectory`.
- After a successful import the original backup file is copied to `data_dir/backups/`
  (kept for recovery).

## 2. Source reading (`source.py`)

Open `BACKUP` read-only (`file:...?mode=ro`, uri=True). Relevant MyExpenses tables
(schema version ~191): `accounts`, `transactions`, `categories` (tree via `parent_id`),
`payee`, `tags`, `transactions_tags`, `paymentmethods`, `currency` (`fraction_digits`).

Transaction rules:
- `amount`, `original_amount` are integers in **minor units** of the account currency resp.
  `original_currency`; divide by `10**fraction_digits` from `currency` (default 2).
- `date` is unix seconds UTC → convert to configured timezone, keep local date (`YYYY-MM-DD`)
  plus `year`, `month` (`YYYY-MM`).
- `status`: 0/1 normal, 2 uncommitted (skip), 3 export helper (skip), 4 archive parent
  (skip — its children carry the data), 5 archived (keep as normal).
- Splits: a parent has children (`parent_id` set) and category label `__SPLIT_TRANSACTION__`;
  emit the children only, inheriting date/account/payee/tags/comment from the parent when
  missing on the child. Archive parents (status 4) also have children; same logic, children
  are not splits.
- Transfers between accounts: `transfer_peer`/`transfer_account` not null → `kind = transfer`.
- Category path: `"Top"` or `"Top > Sub"` (deeper levels joined by `" > "`), plus
  `category_top` and `category_sub`.
- Tags: list of labels per transaction (split children: own tags ∪ parent tags).
- Stable identity key: the real schema only has `UNIQUE(account_id, uuid, status)`, and both legs
  of a transfer carry the *same* uuid (in different accounts). So the key is the plain `uuid`
  for non-transfer rows and `"<uuid>:<account uuid>"` for transfer rows (`accounts.uuid`, falling
  back to the account `_id` when null). Rows without uuid: synthesize a deterministic key and
  warn (count only). If a key still repeats within one backup, the later rows get
  `#2`, `#3`, ... appended (deterministic by row id) and are counted in the stats
  (`duplicate_keys`, warned by the CLI); the importer never crashes on it. Overrides in
  `rules.yaml` match on this key (for a transfer leg: `<uuid>:<account uuid>`).

Normalized source record:

| field | type | notes |
|---|---|---|
| uuid | text | primary identity |
| date | text | local date `YYYY-MM-DD` |
| account | text | account label |
| account_currency | text | ISO code |
| amount | real | in account currency, negative = outflow |
| original_currency | text? | |
| original_amount | real? | |
| category_path, category_top, category_sub | text? | |
| payee | text? | |
| comment | text? | |
| method | text? | payment method label |
| tags | list[str] | |
| is_transfer | bool | app-level transfer |

## 3. Analysis DB and merge (`store.py`)

Tables (SQLite, managed with `sqlite-utils`):

- `transactions` — one row per uuid with all normalized fields (tags stored as JSON array) plus
  `first_seen_backup`, `last_seen_backup`, `deleted_at` (NULL while live).
- `transaction_tags(uuid, tag)` — for faceting/joins.
- `imports(id, backup_file, backup_timestamp, imported_at, min_date, max_date, inserted,
  updated, unchanged, kept_missing, dropped)`.
- `fx_rates(date, currency, rate_per_eur)` — ECB: 1 EUR = rate units of currency.

Merge of a backup B with date range [min_date(B), max_date(B)]:
- uuid in B → insert, or update if any field changed (`last_seen_backup` = B).
- uuid not in B, local `date < min_date(B)` → **keep** (app data was deleted/archived or the
  app was started fresh). Counted as `kept_missing`.
- uuid not in B, local `date >= min_date(B)` → **drop**: set `deleted_at` (soft delete, kept
  for audit, excluded from all views). Counted as `dropped`.
- A previously dropped uuid that reappears is revived.
- Importing the same or an older backup again is idempotent / harmless: skip if the backup
  timestamp is ≤ the latest imported one unless `--force`.
- Older-backup guard: a backup is older when its timestamp is earlier than the latest import's
  (e.g. with `--force`), or, when either timestamp is unknown, when its `max_date` is earlier
  than the latest imports' `max_date`. Comparing dates alone would misfire when the newest
  entry was deleted in the app. For an older backup nothing is dropped:
  rows missing from it are counted as `kept_missing` and `MergeResult.older_backup` is set; the
  CLI prints a warning. Inserts/updates still apply.

## 4. FX (`fx.py`)

- Source: ECB Data Portal, `https://data-api.ecb.europa.eu/service/data/EXR/D.<CUR1+CUR2>.EUR.SP00.A?startPeriod=...&endPeriod=...&format=csvdata`.
- Fetch only missing ranges for the currencies present (non-EUR account and original
  currencies). Cache in `fx_rates`. Offline / failure → keep going, affected rows get
  `amount_eur = NULL` and are counted in the report.
- Lookup for date d: rate on d, else the latest earlier rate (weekends/holidays), else the
  earliest later one if d precedes all data. If that rate is more than `MAX_RATE_GAP_DAYS`
  (10) days away from d there is no usable rate (`None`, row counted as missing EUR).
- `fx_meta` records per currency the last day the tail was checked and the earliest start
  already requested (`earliest_start`); a head range is not requested again when ECB had no
  earlier data.
- Only *account* currencies other than EUR are fetched (an original currency matters only when
  it is EUR, which needs no rate). Unexpected errors in the fetch become `FxError` (a warning).
- Currencies ECB does not publish (e.g. UZS) are only a problem when they are the *account*
  currency; when they are the original currency of a row in a EUR account no rate is needed.

## 5. EUR conversion

```
amount_eur = amount                                   if account_currency == 'EUR'
           = copysign(|original_amount|, amount)      elif original_currency == 'EUR'
           = amount / rate(account_currency, date)    otherwise
```

Original amounts and currencies are always kept.

## 6. Rules (`rules.yaml`, `rules.py`)

Plain YAML outside the repo. Applied in this order when building `expenses`; later steps see
the results of earlier ones:

1. `account_aliases` — rename the account (case-insensitive key).
2. `payee_rules` — the **first** matching rule wins; its `set` is applied.
3. `transfer_rules` — the first matching rule marks the row as transfer.
4. `overrides` — by identity key (see §2); `set` incl. `kind`, `one_off`, `outlier`.

Then EUR conversion, dimensions, one-off flags and outlier detection run. All matches are
case-insensitive, as are the dimension tag lookups (`work_tags`, `country_tag`,
`extra_trip_tags`) and `add_tags`/`remove_tags` (no duplicates differing only in case; the trip
pattern is used as written); `payee`/`comment` accept an exact
string or `regex:` prefix. Everything optional. See `rules.example.yaml`.

```yaml
dimensions:
  work_tags: [WorkTag]              # tag(s) marking work-related spending → is_work
  country_tag: HomeCountryTag       # tag marking life in the explicitly tracked country → in_country
  country_name: Country             # label used in dashboards
  trip_tag_pattern: '^[A-Z][A-Za-z]+(\d{2}|\d{4})$'   # Name+Year tags → trip
  extra_trip_tags: [SomePlace]

payee_rules:                         # fix coarse/missing classification
  - match: {payee: "Some Tax Office"}
    set: {category: "Tasse > Tassa sul reddito"}

transfer_rules:                      # money moved, not spent → kind = transfer
  - match: {payee: "Some Person", category: "Famiglia"}

account_aliases:                     # treat an account as another one (misclassification)
  "Wrong Account": "EUR"

one_off:
  uncategorized_threshold_eur: 10000 # uncategorized expense with |amount_eur| ≥ this → one_off
  categories: ["Tasse"]              # top-level or full path → one_off
  # manual: via overrides

outliers:
  method: mad                        # per category_path, robust z-score on log(spend_eur)
  threshold: 3.5
  min_samples: 8

overrides:                           # by identity key (plain uuid; transfer legs: "<uuid>:<account uuid>")
  - uuid: 00000000-0000-0000-0000-000000000000
    set: {category: "Regali", payee: "Shop", one_off: true, add_tags: [X], kind: expense}
    # also: outlier: false (dismiss a flagged outlier)
```

Validation: unknown keys → error with the path; bad regex → error.

## 7. Derived table `expenses` (`build.py`)

Rebuilt from scratch on every run from live `transactions` + rules + fx. Columns:

`uuid, date, year, month, account, account_currency, amount, original_currency,
original_amount, amount_eur, spend_eur (= -amount_eur, positive for spending), kind
(expense | income | transfer), category_path, category_top, category_sub, payee, comment,
tags (JSON), is_work, in_country, trip (trip tag or NULL), one_off, outlier, rule_notes`.

- `kind`: app transfer or transfer rule → `transfer`; else `amount < 0` → `expense`;
  else `income` (positive entries such as refunds; they net against spending in category
  totals since `spend_eur` is negative for them).
- `outlier`: among `kind = expense` rows of the same `category_path` with ≥ `min_samples`
  rows, robust z = 0.6745·(x − median)/MAD on `ln(spend_eur)` (amounts are right-skewed); flagged when z > threshold
  (only high side). If MAD is 0 (most values identical) the scale falls back to
  1.2533 × the mean absolute deviation of the log values; if that is 0 too, nothing is flagged.
  Override can force true/false. One-off and outlier detection use expense rows only.
- Indexes on date, month, category_top, kind; full-text search on payee/comment/category.
- Convenience views: `spending` (kind in expense, income — refunds net against spending),
  `spending_recurring` (the same and not one_off), `transfers`, `uncategorized`, `flagged`
  (one_off or outlier).

## 8. UI (Datasette)

Spending queries (canned queries, dashboard charts, views) use `kind IN ('expense','income')`:
positive entries (refunds) have a negative `spend_eur` and net against spending. The one-offs
and outliers lists stay on expense rows. `include_one_offs`: `0`, `false`, `no`, `off`
(case-insensitive) exclude one-offs, any other value (or empty) includes them. The monthly
average divides by the number of months in the span from the first to the last month with any
row in the selected range. The original-currency breakdown groups by
`coalesce(original_currency, account_currency)` and sums `-coalesce(original_amount, amount)`;
"rows missing EUR amount" honours `from`/`to`.

Hardening: `expenses serve` passes `--plugins-dir` with a small plugin (`host_check.py`) that
answers 403 unless the Host header is `127.0.0.1`, `localhost` or `[::1]` (any port), against
DNS rebinding. The Datasette subprocess gets the environment without
`EXPENSES_BACKUP_PASSWORD`. Dependencies are pinned to `datasette>=0.65,<1.0`,
`datasette-dashboards>=0.7,<0.9`. The dashboard "flagged" table (library `table`, which renders
HTML and crashes on empty results) HTML-escapes every text column in SQL and always returns at
least one row (a "no flagged rows" placeholder).

`expenses serve` (and the last step of `expenses run`) launches Datasette on
`127.0.0.1:8001` with a generated `metadata.yaml`/`datasette.yaml` (shipped in the package):

- Canned queries (with parameters `from`, `to`, `include_one_offs`): spending by month ×
  top category, by category path for a period, by tag, work spending by month, country vs
  not-country by month, per-trip totals (with date span), top payees, uncategorized rows,
  one-offs, outliers, transfers, original-currency breakdown, rows missing EUR amount.
- Dashboard (datasette-dashboards) "Overview": KPIs (total spend, monthly average), monthly
  spend stacked by top category, category breakdown, work vs non-work, country vs
  not-country, trips, top payees, flagged rows; filters: date range, one-offs on/off.
- Facets configured on `expenses`: kind, category_top, account, year, is_work, in_country,
  trip, one_off, outlier.

## 9. CLI

```
expenses run      [--backup FILE] [--no-serve] [--force] [--offline]   # import + build + serve
expenses import   [--backup FILE] [--force] [--offline]
expenses build    [--offline]          # re-apply rules/fx without importing
expenses serve    [--port N]
expenses check-rules                   # validate rules.yaml
```

Errors (`BackupError`, rules/config errors, `sqlite3.Error` e.g. a corrupt BACKUP, `OSError`)
print one `error: ...` line to stderr, exit 1, no traceback and no row data; Ctrl-C exits 130;
any other exception prints `error: unexpected <ExceptionType>` only.

Report printed after import/build: backup used, counts (inserted/updated/unchanged/kept/
dropped), rows without EUR amount, number of one-offs/outliers. No row-level data unless
`--verbose`.

## 10. Known data limitations

- Cash withdrawn and spent without being recorded is not in the data; nothing to correct
  historically. If withdrawals are recorded in the future as transfers into a cash account,
  a "cash withdrawn vs logged" view can be added.
- Day precision is approximate (timezone fixed); analyses are monthly or coarser.
