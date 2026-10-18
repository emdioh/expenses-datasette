"""Command line interface: `expenses run|import|build|serve|check-rules`."""

from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

from . import backup as backup_mod
from . import fx, store
from .backup import BackupError
from .build import BuildResult, build
from .config import Config, ConfigError, load_config
from .datasette import datasette_args
from .rules import Rules, RulesError, load_rules
from .source import read_source

PASSWORD_ENV = "EXPENSES_BACKUP_PASSWORD"
DEFAULT_PORT = 8001


class CliError(Exception):
    """User-facing error: message to stderr, exit code 1."""


def _out(msg: str) -> None:
    print(msg)


def _warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


# --- import ------------------------------------------------------------------


def _password_for(path: Path) -> str | None:
    if path.suffix.lower() != ".enc":
        return None
    env = os.environ.get(PASSWORD_ENV)
    if env:
        return env
    if sys.stdin is not None and sys.stdin.isatty():
        return getpass.getpass("Backup password: ")
    raise CliError(f"backup is encrypted: set {PASSWORD_ENV} or run from a terminal")


def do_import(cfg: Config, backup: Path | None, force: bool, verbose: bool) -> store.MergeResult:
    path = Path(backup).expanduser() if backup else backup_mod.find_latest_backup(cfg.backup_dir)
    if not path.is_file():
        raise BackupError(f"backup file not found: {path}")
    _out(f"backup: {path.name}")
    password = _password_for(path)
    with backup_mod.open_backup(path, password) as opened:
        home = backup_mod.read_home_currency(opened.prefs_path)
        if home and home != "EUR":
            _out(f"note: home currency in the app is {home}; amounts are converted to EUR anyway")
        data = read_source(opened.db_path, tz=cfg.timezone)
        s = data.stats
        _out(f"source: {s.read} transactions read; {s.splits_expanded} splits expanded")
        if s.missing_uuid:
            _warn(f"{s.missing_uuid} transactions had no uuid (synthetic keys used)")
        conn = store.open_db(cfg.db_path)
        try:
            result = store.merge(
                conn, data, backup_name=path.name,
                backup_timestamp=opened.timestamp, force=force,
            )
            if verbose and result.dropped:
                rows = conn.execute(
                    "SELECT uuid, date FROM transactions WHERE deleted_at = "
                    "(SELECT MAX(deleted_at) FROM transactions)"
                ).fetchall()
                for r in rows:
                    _out(f"  dropped: {r['uuid']} {r['date']}")
        finally:
            conn.close()
    if result.skipped:
        if result.skip_reason == "empty_source":
            _warn("backup contains no transactions; nothing imported")
        else:
            _out("import: skipped (backup is not newer than the last import; use --force)")
        return result
    _out(
        f"merge: {result.inserted} inserted, {result.updated} updated, "
        f"{result.unchanged} unchanged, {result.kept_missing} kept (older than backup range), "
        f"{result.dropped} dropped (deleted in app)"
    )
    if result.dropped:
        _out(f"note: {result.dropped} transactions are no longer in the backup and were marked deleted")
    archived = backup_mod.archive_backup(path, cfg.data_dir)
    _out(f"archived: {archived.parent.name}/{archived.name}")
    return result


# --- build -------------------------------------------------------------------


def _update_fx(conn, offline: bool) -> None:
    if offline:
        _out("fx: offline, using cached rates")
        return
    curs = {
        r[0]
        for r in conn.execute(
            "SELECT account_currency FROM transactions WHERE deleted_at IS NULL "
            "UNION SELECT original_currency FROM transactions "
            "WHERE deleted_at IS NULL AND original_currency IS NOT NULL"
        )
    } - {"EUR"}
    span = conn.execute(
        "SELECT MIN(date), MAX(date) FROM transactions WHERE deleted_at IS NULL"
    ).fetchone()
    if not curs or span[0] is None:
        _out("fx: no foreign currencies")
        return
    res = fx.update_rates(
        conn, sorted(curs), date.fromisoformat(span[0]), date.fromisoformat(span[1]),
        fetcher=fx.fetch_ecb,  # looked up at call time (patchable)
    )
    _out(f"fx: {res.fetched_rows} rates fetched for {len(curs)} currencies")
    for err in res.errors:
        _warn(f"fx fetch failed: {err}")
    if res.unsupported:
        _warn(f"no ECB rates for: {', '.join(sorted(set(res.unsupported)))}")


def _report_build(res: BuildResult) -> None:
    kinds = ", ".join(
        f"{res.by_kind.get(k, 0)} {k}" for k in ("expense", "income", "transfer")
    )
    _out(
        f"build: {res.rows} rows ({kinds}); {res.missing_eur} without EUR; "
        f"{res.one_offs} one-offs; {res.outliers} outliers"
    )


def do_build(cfg: Config, offline: bool, verbose: bool) -> BuildResult:
    rules = load_rules(cfg.rules)
    if not cfg.db_path.is_file():
        raise CliError("no analysis database yet: run `expenses import` first")
    conn = store.open_db(cfg.db_path)
    try:
        store.ensure_schema(conn)
        _update_fx(conn, offline)
        res = build(conn, rules, fx.RateLookup(conn))
        if verbose and res.missing_eur:
            for r in conn.execute("SELECT uuid, date, account_currency FROM expenses "
                                  "WHERE amount_eur IS NULL"):
                _out(f"  no EUR amount: {r['uuid']} {r['date']} {r['account_currency']}")
    finally:
        conn.close()
    _report_build(res)
    return res


# --- serve / check-rules -----------------------------------------------------


def do_serve(cfg: Config, port: int) -> int:
    if not cfg.db_path.is_file():
        raise CliError("no analysis database yet: run `expenses import` first")
    _out(f"serving http://127.0.0.1:{port}/ (Ctrl-C to stop)")
    sys.stdout.flush()
    try:
        return subprocess.run(
            [sys.executable, "-m", "datasette", *datasette_args(cfg.db_path, port)]
        ).returncode
    except KeyboardInterrupt:
        return 0


def do_check_rules(cfg: Config) -> Rules:
    if not Path(cfg.rules).is_file():
        _out(f"rules: {cfg.rules} not found; defaults apply")
    rules = load_rules(cfg.rules)
    _out(
        "rules ok: "
        f"{len(rules.payee_rules)} payee rules, {len(rules.transfer_rules)} transfer rules, "
        f"{len(rules.account_aliases)} account aliases, {len(rules.overrides)} overrides, "
        f"{len(rules.dimensions.work_tags)} work tags, "
        f"{len(rules.one_off.categories)} one-off categories"
    )
    return rules


# --- argument parsing ----------------------------------------------------------


def _common() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=argparse.SUPPRESS, metavar="PATH",
                        help="config file (default ~/.config/expenses/config.toml)")
    common.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS,
                        help="also list individual rows (may show personal data)")
    return common


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="expenses", description="Analyse MyExpenses backups with Datasette.",
        parents=[_common()],
    )
    p.set_defaults(config=None, verbose=False)
    sub = p.add_subparsers(dest="command", required=True)

    def add(name: str, help: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help, parents=[_common()])

    def imp_flags(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--backup", type=Path, metavar="FILE",
                        help="backup file (default: newest in backup_dir)")
        sp.add_argument("--force", action="store_true",
                        help="import even if the backup is not newer")
        sp.add_argument("--offline", action="store_true", help="do not fetch FX rates")

    r = add("run", "import + build + serve")
    imp_flags(r)
    r.add_argument("--no-serve", action="store_true", help="do not start Datasette")
    r.add_argument("--port", type=int, default=DEFAULT_PORT)
    imp_flags(add("import", "merge the newest backup into the database"))
    add("build", "re-apply rules and FX without importing").add_argument(
        "--offline", action="store_true", help="do not fetch FX rates")
    add("serve", "start Datasette").add_argument("--port", type=int, default=DEFAULT_PORT)
    add("check-rules", "validate the rules file")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
        cmd = args.command
        if cmd == "check-rules":
            do_check_rules(cfg)
        elif cmd == "import":
            do_import(cfg, args.backup, args.force, args.verbose)
        elif cmd == "build":
            do_build(cfg, args.offline, args.verbose)
        elif cmd == "serve":
            return do_serve(cfg, args.port)
        elif cmd == "run":
            load_rules(cfg.rules)  # fail early, before touching the database
            do_import(cfg, args.backup, args.force, args.verbose)
            do_build(cfg, args.offline, args.verbose)
            if not args.no_serve:
                return do_serve(cfg, args.port)
    except (BackupError, RulesError, ConfigError, CliError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
