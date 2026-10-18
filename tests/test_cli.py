import sqlite3
import zipfile
from datetime import date, datetime
from pathlib import Path

import pytest

from expenses import backup, cli, fx
from synthetic import SyntheticDB

PASSWORD = "s3cret-pw-xyz"


def make_backup(dir_: Path, stamp: str, rows: list[tuple], enc: bool = False) -> Path:
    """rows: (account_currency, date, amount, category, extra kwargs)."""
    work = dir_ / f"work-{stamp}"
    work.mkdir(parents=True)
    db = SyntheticDB(work / "BACKUP")
    db.add_account("Main", "EUR")
    db.add_account("Swiss", "CHF")
    for acct, when, amount, cat, kw in rows:
        db.add_transaction(
            acct, when, amount, category=cat, uuid=f"u-{acct}-{when}", **kw
        )
    db.close()
    (work / "BACKUP_PREF").write_text(
        '<map><string name="home_currency">USD</string></map>'
    )
    zpath = dir_ / f"backup-{stamp}.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.write(work / "BACKUP", "BACKUP")
        zf.write(work / "BACKUP_PREF", "BACKUP_PREF")
    if enc:
        enc_path = dir_ / f"backup-{stamp}.enc"
        enc_path.write_bytes(backup.encrypt_backup(zpath.read_bytes(), PASSWORD))
        zpath.unlink()
        return enc_path
    return zpath


BASE_ROWS = [
    ("Main", date(2025, 1, 5), -20.0, "Food > Shop", {"payee": "ShopA"}),
    ("Main", date(2025, 1, 6), -35.5, "Food > Shop", {"payee": "ShopB"}),
    ("Main", date(2025, 1, 7), 100.0, "Salary", {}),
    ("Swiss", date(2025, 1, 8), -50.0, "Travel", {}),
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    backups.mkdir()
    data = tmp_path / "data"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'backup_dir = "{backups}"\ndata_dir = "{data}"\n'
        f'rules = "{tmp_path / "rules.yaml"}"\ntimezone = "Europe/Rome"\n'
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("EXPENSES_CONFIG", raising=False)
    monkeypatch.delenv(cli.PASSWORD_ENV, raising=False)
    return type("Env", (), {"tmp": tmp_path, "backups": backups, "data": data, "cfg": cfg})


def run(env, capsys, *args):
    code = cli.main(["--config", str(env.cfg), *args])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def test_run_end_to_end(env, capsys):
    zpath = make_backup(env.backups, "20250110-120000", BASE_ROWS)
    code, out, err = run(env, capsys, "run", "--no-serve", "--offline")
    assert code == 0, err
    assert "backup: backup-20250110-120000.zip" in out
    assert "merge: 4 inserted, 0 updated, 0 unchanged" in out
    assert "build: 4 rows (3 expense, 1 income, 0 transfer); 1 without EUR" in out
    assert "note: home currency in the app is USD" in out
    assert (env.data / "expenses.db").is_file()
    assert (env.data / "backups" / zpath.name).is_file()
    assert "ShopA" not in out + err


def test_second_run_skipped_and_force(env, capsys):
    make_backup(env.backups, "20250110-120000", BASE_ROWS)
    assert run(env, capsys, "run", "--no-serve", "--offline")[0] == 0
    code, out, _ = run(env, capsys, "run", "--no-serve", "--offline")
    assert code == 0
    assert "skipped" in out and "merge:" not in out
    assert "build: 4 rows" in out  # still builds
    code, out, _ = run(env, capsys, "run", "--no-serve", "--offline", "--force")
    assert "merge: 0 inserted, 0 updated, 4 unchanged" in out


def test_deletion_reported(env, capsys):
    make_backup(env.backups, "20250110-120000", BASE_ROWS)
    run(env, capsys, "import", "--offline")
    make_backup(env.backups, "20250120-120000", BASE_ROWS[:2] + BASE_ROWS[3:])
    code, out, _ = run(env, capsys, "import")
    assert code == 0
    assert "1 dropped" in out and "marked deleted" in out
    code, out, _ = run(env, capsys, "build", "--offline")
    assert "build: 3 rows" in out


def test_encrypted_backup(env, capsys, monkeypatch):
    make_backup(env.backups, "20250110-120000", BASE_ROWS, enc=True)
    monkeypatch.setenv(cli.PASSWORD_ENV, PASSWORD)
    code, out, err = run(env, capsys, "import")
    assert code == 0, err
    assert "backup-20250110-120000.enc" in out
    assert PASSWORD not in out + err
    assert (env.data / "backups" / "backup-20250110-120000.enc").is_file()


def test_wrong_password(env, capsys, monkeypatch):
    make_backup(env.backups, "20250110-120000", BASE_ROWS, enc=True)
    monkeypatch.setenv(cli.PASSWORD_ENV, "wrong-pw-123")
    code, out, err = run(env, capsys, "import")
    assert code == 1
    assert "error:" in err and "Traceback" not in err
    assert "wrong-pw-123" not in out + err and PASSWORD not in out + err
    assert not (env.data / "backups").exists()


def test_encrypted_without_password_non_tty(env, capsys):
    make_backup(env.backups, "20250110-120000", BASE_ROWS, enc=True)
    code, _, err = run(env, capsys, "import")
    assert code == 1 and cli.PASSWORD_ENV in err


def test_no_backup_error(env, capsys):
    code, _, err = run(env, capsys, "import")
    assert code == 1 and "no backup files" in err


def test_check_rules(env, capsys):
    code, out, _ = run(env, capsys, "check-rules")
    assert code == 0 and "not found" in out
    rules = env.tmp / "rules.yaml"
    rules.write_text("payee_rules:\n  - match: {payee: X}\n    set: {category: 'A > B'}\n")
    code, out, _ = run(env, capsys, "check-rules")
    assert code == 0 and "1 payee rules" in out
    rules.write_text("bogus_key: 1\n")
    code, out, err = run(env, capsys, "check-rules")
    assert code == 1 and "bogus_key" in err and "Traceback" not in err


def test_bad_config(env, capsys):
    env.cfg.write_text('nope = "x"\n')
    code, _, err = run(env, capsys, "check-rules")
    assert code == 1 and "nope" in err
    code = cli.main(["--config", str(env.tmp / "missing.toml"), "check-rules"])
    assert code == 1


def test_config_env_and_tilde(env, capsys, monkeypatch):
    from expenses.config import load_config

    p = env.tmp / "alt.toml"
    p.write_text('backup_dir = "~/bk"\n')
    monkeypatch.setenv("EXPENSES_CONFIG", str(p))
    cfg = load_config(None)
    assert cfg.backup_dir == Path.home() / "bk"
    assert cfg.db_path.name == "expenses.db"


def test_build_with_fake_fx(env, capsys, monkeypatch):
    make_backup(env.backups, "20250110-120000", BASE_ROWS)
    calls = []

    def fake(currencies, start, end):
        calls.append(tuple(currencies))
        return [("2025-01-08", "CHF", 0.5), ("2025-01-03", "CHF", 0.5)]

    monkeypatch.setattr(fx, "fetch_ecb", fake)
    code, out, err = run(env, capsys, "run", "--no-serve")
    assert code == 0, err
    assert calls == [("CHF",)]
    assert "0 without EUR" in out
    con = sqlite3.connect(env.data / "expenses.db")
    eur = con.execute("SELECT amount_eur FROM expenses WHERE account='Swiss'").fetchone()[0]
    assert eur == -100.0


def test_fx_failure_is_warning(env, capsys, monkeypatch):
    make_backup(env.backups, "20250110-120000", BASE_ROWS)

    def boom(*a):
        raise fx.FxError("network down")

    monkeypatch.setattr(fx, "fetch_ecb", boom)
    code, out, err = run(env, capsys, "run", "--no-serve")
    assert code == 0
    assert "warning: fx fetch failed" in err
    assert "1 without EUR" in out


def test_serve_command(env, capsys, monkeypatch):
    make_backup(env.backups, "20250110-120000", BASE_ROWS)
    run(env, capsys, "run", "--no-serve", "--offline")
    seen = []

    class P:
        returncode = 0

    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **kw: seen.append(cmd) or P())
    code, out, _ = run(env, capsys, "serve", "--port", "8123")
    assert code == 0
    cmd = seen[0]
    assert "--plugins-dir" in cmd
    assert cmd[1:4] == ["-m", "datasette", "serve"]
    assert str(env.data / "expenses.db") in cmd and "8123" in cmd
    assert "http://127.0.0.1:8123/" in out
    # run without --no-serve also serves
    seen.clear()
    assert run(env, capsys, "run", "--offline")[0] == 0
    assert len(seen) == 1


def test_serve_without_db(env, capsys):
    code, _, err = run(env, capsys, "serve")
    assert code == 1 and "import" in err


def test_explicit_backup_option(env, capsys):
    p = make_backup(env.tmp, "20240101-000000", BASE_ROWS)
    code, out, _ = run(env, capsys, "import", "--backup", str(p))
    assert code == 0 and "backup-20240101-000000.zip" in out


def test_serve_env_has_no_password(env, capsys, monkeypatch):
    make_backup(env.backups, "20250110-120000", BASE_ROWS)
    run(env, capsys, "run", "--no-serve", "--offline")
    monkeypatch.setenv(cli.PASSWORD_ENV, PASSWORD)
    monkeypatch.setenv("KEEP_ME", "1")
    seen = []

    class P:
        returncode = 0

    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **kw: seen.append(kw) or P())
    assert run(env, capsys, "serve")[0] == 0
    assert cli.PASSWORD_ENV not in seen[0]["env"] and seen[0]["env"]["KEEP_ME"] == "1"
    assert PASSWORD not in "".join(seen[0]["env"].values())


def test_fx_only_for_non_eur_account_currencies(env, capsys, monkeypatch):
    rows = [
        ("Main", date(2025, 1, 5), -20.0, "Food", {"original": (3000.0, "JPY")}),
        ("Main", date(2025, 1, 6), -10.0, "Food", {"original": (10.0, "EUR")}),
    ]
    make_backup(env.backups, "20250110-120000", rows)
    calls = []
    monkeypatch.setattr(fx, "fetch_ecb", lambda c, s, e: calls.append(c) or [])
    code, out, err = run(env, capsys, "run", "--no-serve")
    assert code == 0, err
    assert calls == [] and "no foreign currencies" in out and err == ""


def test_older_backup_warning_and_nothing_dropped(env, capsys):
    make_backup(env.backups, "20250110-120000", BASE_ROWS)
    run(env, capsys, "import", "--offline")
    older = make_backup(env.tmp, "20250101-000000", BASE_ROWS[:2])  # older stamp
    code, out, err = run(env, capsys, "import", "--backup", str(older))
    assert code == 0 and "skipped" in out
    code, out, err = run(env, capsys, "import", "--backup", str(older), "--force")
    assert code == 0 and "older backup" in err
    assert "0 dropped" in out and "2 kept" in out


def test_corrupt_backup_file_is_clean_error(env, capsys):
    work = env.tmp / "corrupt"
    work.mkdir()
    (work / "BACKUP").write_bytes(b"this is not a sqlite database" * 50)
    zpath = env.backups / "backup-20250110-120000.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.write(work / "BACKUP", "BACKUP")
    code, out, err = run(env, capsys, "import")
    assert code == 1
    assert err.startswith("error:") and err.count("\n") == 1 and "Traceback" not in err


@pytest.mark.parametrize("exc, code, text", [
    (sqlite3.OperationalError("disk I/O error"), 1, "error: database error: disk I/O error"),
    (PermissionError(13, "Permission denied"), 1, "error: [Errno 13] Permission denied"),
    (KeyboardInterrupt(), 130, "error: interrupted"),
    (RuntimeError("payee ShopA 12.5"), 1, "error: unexpected RuntimeError"),
])
def test_unexpected_errors_are_one_line(env, capsys, monkeypatch, exc, code, text):
    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(cli, "do_build", boom)
    got, out, err = run(env, capsys, "build")
    assert got == code
    assert err.strip() == text and "Traceback" not in err and "ShopA" not in err


def test_non_fx_error_in_fetch_ecb_is_a_warning(env, capsys, monkeypatch):
    make_backup(env.backups, "20250110-120000", BASE_ROWS)

    def boom(*a, **k):
        raise ValueError("secret row detail")

    monkeypatch.setattr(fx.urllib.request, "urlopen", boom)
    code, out, err = run(env, capsys, "run", "--no-serve")
    assert code == 0 and "warning: fx fetch failed" in err
    assert "secret row detail" not in err and "Traceback" not in err
