"""`trade-terminal doctor`: is this installation healthy, and if not, what to do.

Every check returns ok, warn or fail with one line saying what was found and,
when it is not ok, what fixes it. Checks never raise: a broken check reports
itself as a failure. The same report backs GET /api/doctor and the diagnostics
bundle.
"""

from __future__ import annotations

import platform
import shutil
import sqlite3
import stat
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.core.config import HOME, IS_CHECKOUT, IST, ROOT, settings
from backend.core.version import __version__

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str


def ui_dir() -> Path | None:
    """The built UI: packaged inside an installed copy, or built in a checkout."""
    for candidate in (Path(__file__).resolve().parents[1] / "ui", ROOT / "frontend" / "dist"):
        if (candidate / "index.html").exists():
            return candidate
    return None


def _versions() -> Check:
    return Check("version", OK, f"Trade Terminal {__version__}, Python {platform.python_version()}, "
                                f"{platform.system()} {platform.machine()}, "
                                f"{'source checkout' if IS_CHECKOUT else 'installed'}")


def _data_dir() -> Check:
    data = settings.data_dir
    try:
        data.mkdir(parents=True, exist_ok=True)
        probe = data / ".doctor-write-test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        return Check("data directory", FAIL, f"{data} is not writable ({exc})")
    free_gb = shutil.disk_usage(data).free / 1e9
    status = OK if free_gb >= 2 else WARN if free_gb >= 0.5 else FAIL
    return Check("data directory", status, f"{data}, {free_gb:.1f} GB free"
                 + ("" if status == OK else "; free up disk space"))


def _env_file() -> Check:
    path = HOME / ".env"
    if not path.exists():
        return Check("settings file", OK, f"none at {path} (defaults in use)")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        return Check("settings file", WARN, f"{path} is readable by other users (mode {mode:o}); "
                                            f"run: chmod 600 '{path}'")
    return Check("settings file", OK, f"{path} (mode {mode:o})")


def _app_db() -> Check:
    from backend.appstate import db

    try:
        conn = db.connection()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        counts = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                  for t in ("strategies", "runs", "conversations")}
        check = conn.execute("PRAGMA quick_check").fetchone()[0]
    except (sqlite3.Error, RuntimeError) as exc:
        return Check("app database", FAIL, str(exc))
    if check != "ok":
        return Check("app database", FAIL, f"integrity check failed: {check}; restore a backup")
    return Check("app database", OK, f"schema {version}, {counts['strategies']} strategies, "
                                     f"{counts['runs']} runs, {counts['conversations']} conversations")


def _market_store() -> tuple[Check, Check, Check]:
    from backend.data import instruments as inst
    from backend.data.store import all_coverage, get_db

    try:
        get_db()
    except RuntimeError as exc:
        busy = Check("market data store", WARN, str(exc).splitlines()[0]
                     + " (normal if the app is running; checks that need it are skipped)")
        skipped = Check("instrument master", WARN, "skipped: store in use")
        return busy, skipped, Check("stored data", WARN, "skipped: store in use")

    store = Check("market data store", OK, str(settings.db_path))
    try:
        count = inst.count()
        row = get_db().cursor().execute(
            "SELECT value FROM meta WHERE key = 'instruments_synced_at'").fetchone()
        if count == 0:
            master = Check("instrument master", WARN, "not synced; stock symbols cannot be resolved. "
                           "Run: trade-terminal sync-instruments")
        else:
            age = (datetime.now(IST) - datetime.fromisoformat(row[0])).days if row else None
            stale = age is not None and age > 7
            master = Check("instrument master", WARN if stale else OK,
                           f"{count:,} instruments"
                           + (f", synced {age} day(s) ago" if age is not None else "")
                           + ("; contracts change weekly, run: trade-terminal sync-instruments" if stale else ""))
    except Exception as exc:
        master = Check("instrument master", FAIL, str(exc))

    try:
        coverage = all_coverage()
        if coverage.empty:
            data = Check("stored data", OK, "none yet; it is downloaded when first needed")
        else:
            newest = str(coverage["last_bar"].max())[:10]
            data = Check("stored data", OK, f"{len(coverage)} dataset(s), newest bar {newest}")
    except Exception as exc:
        data = Check("stored data", FAIL, str(exc))
    return store, master, data


def _broker() -> Check:
    from backend.data.upstox_auth import token_status

    status = token_status()
    if status.valid:
        hours = status.seconds_remaining / 3600
        return Check("Upstox", OK, f"connected, token valid for {hours:.1f} h")
    return Check("Upstox", WARN, f"not connected: {status.reason}. Backtests on stored or "
                                 f"synthetic data still work.")


def _llm() -> Check:
    from backend.llm import factory

    status = factory.status()
    if status["configured"]:
        return Check("strategy assistant", OK, f"{status['model']}, key from {status['key_source']}")
    return Check("strategy assistant", WARN, f"no API key for {status['provider']}; add one in the "
                                             f"Assistant tab. The manual builder works without it.")


def _risk() -> Check:
    if settings.max_daily_loss is None:
        return Check("account risk limits", WARN, "MAX_DAILY_LOSS is not set. Fine for backtests and "
                                                  "paper; live trading is blocked until it is.")
    return Check("account risk limits", OK, f"max daily loss {settings.max_daily_loss:,.0f}, "
                                            f"max positions {settings.max_concurrent_positions}")


def _ui() -> Check:
    found = ui_dir()
    if found:
        return Check("user interface", OK, f"bundled at {found}")
    return Check("user interface", WARN, "no built UI found; run: cd frontend && npm ci && npm run build "
                                         "(or use the Vite dev server)")


def _backups() -> Check:
    from backend.ops import backup

    newest = backup.latest()
    if newest is None:
        return Check("backups", WARN, "none yet; one is taken at the next start, or run: "
                                      "trade-terminal backup")
    age_h = (datetime.now().timestamp() - newest.stat().st_mtime) / 3600
    try:
        backup.verify(newest)
    except backup.BackupError as exc:
        return Check("backups", FAIL, str(exc))
    status = OK if age_h < 72 else WARN
    return Check("backups", status, f"{len(backup.list_backups())} kept, newest {newest.name} "
                                    f"({age_h:.0f} h old, verified)")


def _logs() -> Check:
    path = settings.logs_dir / "terminal.log"
    if not path.exists():
        return Check("logs", OK, f"none yet ({settings.logs_dir})")
    errors = 0
    with open(path, errors="replace") as fh:
        for line in fh.readlines()[-2000:]:
            if " ERROR " in line or " CRITICAL " in line:
                errors += 1
    return Check("logs", WARN if errors else OK,
                 f"{path}" + (f"; {errors} error line(s) in the recent log" if errors else ""))


def run() -> list[Check]:
    checks: list[Check] = []
    simple: list[Callable[[], Check]] = [_versions, _data_dir, _env_file, _app_db]
    for fn in simple:
        checks.append(_safe(fn))
    try:
        checks.extend(_market_store())
    except Exception as exc:
        checks.append(Check("market data store", FAIL, f"{type(exc).__name__}: {exc}"))
    for fn in (_broker, _llm, _risk, _ui, _backups, _logs):
        checks.append(_safe(fn))
    return checks


def _safe(fn: Callable[[], Check]) -> Check:
    try:
        return fn()
    except Exception as exc:
        return Check(fn.__name__.strip("_").replace("_", " "), FAIL, f"{type(exc).__name__}: {exc}")


def report() -> dict[str, Any]:
    checks = run()
    worst = FAIL if any(c.status == FAIL for c in checks) else (
        WARN if any(c.status == WARN for c in checks) else OK)
    return {"status": worst, "checks": [asdict(c) for c in checks],
            "generated_at": datetime.now(IST).isoformat(timespec="seconds")}


def render(checks: list[Check]) -> str:
    marks = {OK: "ok  ", WARN: "WARN", FAIL: "FAIL"}
    width = max(len(c.name) for c in checks)
    return "\n".join(f"[{marks[c.status]}] {c.name:<{width}}  {c.detail}" for c in checks)


def exit_code(checks: list[Check]) -> int:
    return 1 if any(c.status == FAIL for c in checks) else 0


if __name__ == "__main__":
    result = run()
    print(render(result))
    sys.exit(exit_code(result))
