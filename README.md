# expenses-datasette

Analyse personal expenses recorded with the [MyExpenses](https://github.com/mtotschnig/MyExpenses)
Android app. One command picks up the newest MyExpenses backup, merges it into a persistent
local SQLite database, converts every amount to EUR with ECB reference rates (original
currency is kept), applies your correction rules and serves the result with
[Datasette](https://datasette.io), including a dashboard and canned queries.

Design details: [DESIGN.md](DESIGN.md).

## Privacy

Everything stays on your machine. The repository contains no backups, databases or real rules;
tests use synthetic data only. Reports print counts and totals, never transaction details
(unless you pass `--verbose`). The backup password is read from the environment or prompted,
and is never written to disk or logged. Keep your real `config.toml` and `rules.yaml` outside
the repository (`~/.config/expenses/`).

## Install

```sh
uv sync            # inside the repository
uv run expenses --help
# or install the command: uv tool install .
```

Requires Python 3.11+.

## Setup

1. In MyExpenses enable **automatic backup** (Settings, Backup/Restore) and optionally set a
   backup password.
2. Sync the phone's MyExpenses backup folder to `~/MyExpensesBackups` on your computer with
   [Syncthing](https://syncthing.net) (or any other tool). Files are named
   `backup-YYYYMMDD-HHMMSS.zip` (or `.enc` when encrypted).
3. Create `~/.config/expenses/config.toml` (all keys optional):

```toml
backup_dir = "~/MyExpensesBackups"
data_dir   = "~/.local/share/expenses"      # expenses.db + archived backups
rules      = "~/.config/expenses/rules.yaml"
timezone   = "Europe/Rome"
```

   Unknown keys are an error. Set `EXPENSES_CONFIG` or pass `--config PATH` to use another file.
4. For encrypted backups export the password, or just wait for the prompt:
   `export EXPENSES_BACKUP_PASSWORD=...` (non-interactive runs need the variable).

## Rules

Rules fix classifications that the app cannot express: payee-based recategorisation,
transfers that are not recorded as such, account aliases, one-off and outlier detection,
trip/work/country dimensions and per-transaction overrides. Copy
[`rules.example.yaml`](rules.example.yaml) to `~/.config/expenses/rules.yaml` and edit it.
Validate with `expenses check-rules`. A missing rules file means defaults.

## Usage

```
expenses [--config PATH] [--verbose] <command>

expenses run     [--backup FILE] [--no-serve] [--force] [--offline] [--port N]
expenses import  [--backup FILE] [--force] [--offline]
expenses build   [--offline]
expenses serve   [--port N]
expenses check-rules
```

- `import`: takes the newest backup (or `--backup FILE`), merges it into the database and copies
  the backup to `data_dir/backups/`. A backup that is not newer than the last import is skipped
  unless `--force`. Transactions that vanished from the app within the backup's date range are
  soft-deleted (reported as "dropped"); older ones are kept. A backup that ends before data
  already imported (older backup) never drops anything and prints a warning.
- `build`: fetches missing ECB rates for the currencies present (skipped with `--offline`; failures
  are warnings, affected rows get no EUR amount), then rebuilds the derived `expenses` table
  from transactions, rules and rates. Use it after editing the rules.
- `serve`: starts Datasette on <http://127.0.0.1:8001/> (`--port` to change).
- `run`: import, build and serve in one go (`--no-serve` to stop before serving).
- `check-rules`: validates the rules file and prints counts.

Sample output:

```
backup: backup-20250101-120000.zip
merge: 10 inserted, 0 updated, 0 unchanged, 0 kept (older than backup range), 0 dropped (deleted in app)
build: 4300 rows (4290 expense, 3 income, 7 transfer); 0 without EUR; 26 one-offs; 58 outliers
```

Errors (bad config, wrong password, invalid rules, no backup, corrupt backup, I/O problems) print
one line to stderr and exit with status 1 (130 on Ctrl-C); no traceback, no row data.

## Weekly refresh

Refresh the data automatically and start the UI only when you want to look at it. With cron:

```cron
30 7 * * 1  expenses import && expenses build
```

or a systemd user timer: a `expenses-refresh.service` with
`ExecStart=%h/.local/bin/expenses import` followed by `build` (e.g. two `ExecStart=` lines, or
`/bin/sh -c 'expenses import && expenses build'`) and an `expenses-refresh.timer` with
`OnCalendar=weekly` and `Persistent=true`, enabled via `systemctl --user enable --now
expenses-refresh.timer`. For encrypted backups provide `EXPENSES_BACKUP_PASSWORD` through the
service's `EnvironmentFile=` (a file readable only by you). Run `expenses serve` on demand.

## Dashboards and queries

Datasette shows the `expenses` table (faceted by kind, category, account, year, work, country,
trip, one-off, outlier; full-text search) and convenience views: `spending`,
`spending_recurring`, `transfers`, `uncategorized`, `flagged`. Canned queries cover spending by
month and category, by period, by tag, work spending, country versus not-country, per-trip
totals, top payees, uncategorized rows, one-offs, outliers, transfers, original-currency
breakdown and rows missing an EUR amount. Refunds (positive entries) net against spending.
`include_one_offs` accepts 1/0 (also false/no/off to exclude). The server only answers requests
with a loopback Host header (127.0.0.1, localhost, [::1]). The "Overview" dashboard
(datasette-dashboards) shows KPIs and charts with date-range and one-off filters.

## Limitations

- Cash that is withdrawn and spent without being recorded is not in the data.
- Day precision is approximate (fixed timezone); analyses are best at monthly or coarser level.
- Currencies the ECB does not publish only matter when they are an account's currency; those
  rows have no EUR amount and are counted in the report.

## Development

```sh
uv run pytest -q
```
