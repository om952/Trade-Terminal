"""P2.4 / P2.7: packaging support, doctor, backups, diagnostics."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from backend import cli
from backend.appstate import db
from backend.appstate import strategies as saved
from backend.core import logsetup
from backend.core.config import IST, ROOT, settings
from backend.ops import backup, diagnostics, doctor
from backend.strategy import templates


@pytest.fixture
def home(tmp_path, monkeypatch):
    from backend.data import store

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store.close_db()
    yield tmp_path
    db.close()
    store.close_db()


def _seed(home) -> str:
    version, _ = saved.save(templates.load("ema_crossover").model_dump(mode="json"))
    (home / "journal").mkdir(exist_ok=True)
    (home / "journal" / "20250101-X-abc.jsonl").write_text('{"seq":1}\n')
    return version.strategy_id


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------

def test_backup_round_trip_restores_strategies_and_journals(home, tmp_path_factory, monkeypatch):
    strategy_id = _seed(home)
    archive = backup.create()
    assert archive is not None and archive.stat().st_mode & 0o777 == 0o600
    manifest = backup.verify(archive)
    assert set(manifest["files"]) == {"app.sqlite", "journal/20250101-X-abc.jsonl"}

    # A new, empty installation.
    db.close()
    fresh = tmp_path_factory.mktemp("fresh")
    monkeypatch.setattr(settings, "data_dir", fresh)
    restored = backup.restore(archive)
    assert "app.sqlite" in restored
    assert saved.get(strategy_id).document["name"] == templates.load("ema_crossover").name
    assert (fresh / "journal" / "20250101-X-abc.jsonl").read_text() == '{"seq":1}\n'


def test_backup_is_a_consistent_snapshot_while_the_db_is_in_use(home):
    _seed(home)
    conn = db.connection()
    conn.execute("BEGIN IMMEDIATE")          # a write in flight, not committed
    conn.execute("UPDATE strategies SET name = 'half-written'")
    archive = backup.create()
    conn.execute("ROLLBACK")
    with zipfile.ZipFile(archive) as zf:
        (home / "check.sqlite").write_bytes(zf.read("app.sqlite"))
    import sqlite3

    copy = sqlite3.connect(home / "check.sqlite")
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert copy.execute("SELECT name FROM strategies").fetchone()[0] != "half-written"
    copy.close()


def test_nothing_to_back_up_writes_nothing(home):
    assert backup.create() is None and backup.list_backups() == []


def test_restore_never_overwrites_without_force(home):
    _seed(home)
    archive = backup.create()
    with pytest.raises(backup.BackupError, match="already exist"):
        backup.restore(archive)
    db.close()
    assert "app.sqlite" in backup.restore(archive, force=True)


def test_a_tampered_backup_fails_verification(home):
    _seed(home)
    archive = backup.create()
    tampered = home / "tampered.zip"
    with zipfile.ZipFile(archive) as src, zipfile.ZipFile(tampered, "w") as dst:
        for name in src.namelist():
            data = src.read(name)
            dst.writestr(name, data + b"x" if name.startswith("journal/") else data)
    with pytest.raises(backup.BackupError, match="does not match"):
        backup.verify(tampered)
    (home / "junk.zip").write_text("not a zip")
    with pytest.raises(backup.BackupError, match="not a readable backup"):
        backup.verify(home / "junk.zip")


def test_old_backups_are_pruned_and_daily_is_due_once(home):
    _seed(home)
    start = datetime(2025, 1, 1, 9, 0, tzinfo=IST)
    for day in range(backup.KEEP + 3):
        backup.create(now=start + timedelta(days=day))
    kept = backup.list_backups()
    assert len(kept) == backup.KEEP
    assert kept[0].name == "backup-20250104-090000.zip", "the oldest three should be gone"

    assert not backup.due(), "one was just written"
    assert backup.create_if_due() is None
    assert backup.due(now=datetime.now(IST) + timedelta(hours=25))


def test_a_failing_backup_does_not_raise_at_startup(home, monkeypatch):
    monkeypatch.setattr(backup, "create", lambda: (_ for _ in ()).throw(OSError("disk full")))
    assert backup.create_if_due() is None


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------

def test_doctor_on_a_fresh_installation_has_no_failures(home):
    checks = doctor.run()
    by_name = {c.name: c for c in checks}
    assert doctor.exit_code(checks) == 0, doctor.render(checks)
    assert by_name["app database"].status == "ok" and "schema" in by_name["app database"].detail
    assert by_name["instrument master"].status == "warn"
    assert "sync-instruments" in by_name["instrument master"].detail
    assert by_name["Upstox"].status == "warn" and by_name["strategy assistant"].status == "warn"
    assert by_name["backups"].status == "warn"
    text = doctor.render(checks)
    assert "[ok  ] version" in text and "[WARN] Upstox" in text


def test_doctor_reports_a_broken_check_instead_of_crashing(home, monkeypatch):
    monkeypatch.setattr(doctor, "_broker", lambda: (_ for _ in ()).throw(ValueError("kaput")))
    checks = doctor.run()
    assert doctor.exit_code(checks) == 1
    assert any(c.status == "fail" and "kaput" in c.detail for c in checks)


def test_doctor_flags_a_world_readable_settings_file(home, monkeypatch):
    monkeypatch.setattr(doctor, "HOME", home)
    env = home / ".env"
    env.write_text("LOG_LEVEL=INFO\n")
    env.chmod(0o644)
    assert doctor._env_file().status == "warn" and "chmod 600" in doctor._env_file().detail
    env.chmod(0o600)
    assert doctor._env_file().status == "ok"


def test_doctor_endpoint(home):
    from backend.api.main import app

    body = TestClient(app, base_url="http://127.0.0.1:8000").get("/api/doctor").json()
    assert body["status"] in ("ok", "warn") and len(body["checks"]) >= 10


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def test_diagnostics_bundle_holds_no_secrets_or_user_work(home, monkeypatch):
    token = "123456789:AAH-secret_secret_secret_secret_secret_1"
    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr(token))
    monkeypatch.setattr(settings, "upstox_api_secret", SecretStr("upstox-super-secret-value"))
    strategy_id = _seed(home)

    root, level = logging.getLogger(), logging.getLogger().level
    try:
        logsetup.configure_logging("INFO")
        logging.getLogger("backend.test").error("send failed for bot%s", token)
        for handler in root.handlers:
            handler.flush()
        bundle = diagnostics.create()
    finally:
        logsetup.reset_logging()
        root.setLevel(level)

    with zipfile.ZipFile(bundle) as zf:
        names = set(zf.namelist())
        everything = "\n".join(zf.read(n).decode() for n in names)
        summary = json.loads(zf.read("settings.json"))
    assert names == {"doctor.json", "packages.json", "settings.json", "jobs.json", "log-tail.txt"}
    assert token not in everything and "upstox-super-secret-value" not in everything
    assert "send failed" in everything, "the log tail should be there, redacted"
    assert summary["telegram_bot_token"] == "set (.env)" and summary["upstox_api_secret"] == "set (.env)"
    assert strategy_id not in everything and "EMA Crossover" not in everything
    assert bundle.stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------------------
# Store version, UI, CLI, home directory
# ---------------------------------------------------------------------------

def test_a_newer_market_store_is_refused(home):
    from backend.data import store

    conn = store.get_db()
    assert conn.execute(
        "SELECT value FROM meta WHERE key = 'store_schema_version'"
    ).fetchone()[0] == str(store.STORE_SCHEMA_VERSION)
    conn.execute("UPDATE meta SET value = '99' WHERE key = 'store_schema_version'")
    store.close_db()
    with pytest.raises(RuntimeError, match="newer than this build"):
        store.get_db()
    with pytest.raises(RuntimeError, match="newer than this build"):
        store.get_db()                       # still refused, not a stale closed handle


def test_the_ui_is_served_next_to_the_api(tmp_path):
    from backend.api.main import mount_ui

    (tmp_path / "index.html").write_text("<!doctype html><title>Trade Terminal</title>")
    application = FastAPI()

    @application.get("/api/health")
    def health():
        return {"status": "ok"}

    assert mount_ui(application, tmp_path) and not mount_ui(FastAPI(), None)
    client = TestClient(application)
    assert "Trade Terminal" in client.get("/").text
    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/api/nope").status_code == 404


def test_cli_commands(home, capsys):
    from backend.core.version import __version__

    assert cli.main(["version"]) == 0 and capsys.readouterr().out.strip() == __version__
    assert cli.main(["--version"]) == 0
    assert cli.main(["where"]) == 0 and str(home) in capsys.readouterr().out
    assert cli.main(["backup"]) == 0 and "Nothing to back up" in capsys.readouterr().out
    assert cli.main(["doctor"]) == 0 and "[ok  ] version" in capsys.readouterr().out

    _seed(home)
    assert cli.main(["backup"]) == 0 and "verified" in capsys.readouterr().out
    archive = backup.latest()
    assert cli.main(["restore", str(archive)]) == 1 and "Not restored" in capsys.readouterr().out
    db.close()
    assert cli.main(["restore", str(archive), "--force"]) == 0
    assert cli.main(["diagnostics"]) == 0 and "no strategies" in capsys.readouterr().out
    logsetup.reset_logging()


def test_home_can_be_moved_with_an_environment_variable(tmp_path):
    code = "from backend.core.config import HOME, settings; print(HOME); print(settings.data_dir)"
    env = {"PATH": os.environ["PATH"], "TRADE_TERMINAL_HOME": str(tmp_path)}
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == [str(tmp_path), str(tmp_path / "data")]
