"""A diagnostics bundle someone can attach to a bug report without leaking anything.

Contains: the doctor report, package versions, which settings are set (never
their values), recent jobs (kind, status and error only) and the tail of the
log, passed through the redactor again.

Deliberately absent: strategies, runs, conversations, journals, market data,
credentials. A bundle must be safe to post in a public issue.
"""

from __future__ import annotations

import importlib.metadata
import json
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.core.config import IST, settings
from backend.core.logsetup import redact
from backend.ops import doctor

LOG_TAIL_LINES = 2000


def settings_summary() -> dict[str, Any]:
    """Non-secret settings as values; secret ones only as set / not set."""
    plain = ("mode", "log_level", "llm_provider", "llm_model", "llm_effort", "llm_max_steps",
             "llm_max_tokens_per_compile", "llm_max_seconds")
    out: dict[str, Any] = {name: str(getattr(settings, name)) for name in plain}
    out["mode"] = settings.mode.value
    from backend.core import credentials

    for item in credentials.status():
        out[str(item["name"])] = f"set ({item['source']})" if item["source"] else "not set"
    for name in ("upstox_api_key", "kite_api_key", "telegram_chat_id", "api_allowed_hosts",
                 "api_allowed_origins"):
        out[name] = "set" if getattr(settings, name) else "not set"
    out["max_daily_loss"] = "set" if settings.max_daily_loss is not None else "not set"
    return out


def _packages() -> dict[str, str]:
    wanted = ("fastapi", "uvicorn", "pydantic", "duckdb", "pandas", "numpy", "pyarrow", "httpx",
              "anthropic", "langgraph", "langgraph-checkpoint-sqlite", "keyring", "websockets")
    out = {}
    for name in wanted:
        try:
            out[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            out[name] = "not installed"
    return out


def _recent_jobs() -> list[dict[str, Any]]:
    from backend.appstate.db import connection

    rows = connection().execute(
        "SELECT kind, status, error, created_at, finished_at FROM jobs ORDER BY created_at DESC LIMIT 30"
    ).fetchall()
    return [dict(r) | {"error": redact(r["error"]) if r["error"] else None} for r in rows]


def _log_tail() -> str:
    path = settings.logs_dir / "terminal.log"
    if not path.exists():
        return ""
    with open(path, errors="replace") as fh:
        return redact("".join(fh.readlines()[-LOG_TAIL_LINES:]))


def create(directory: Path | None = None) -> Path:
    directory = directory or (settings.data_dir / "diagnostics")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"diagnostics-{datetime.now(IST):%Y%m%d-%H%M%S}.zip"
    try:
        jobs = _recent_jobs()
    except Exception as exc:
        jobs = [{"error": f"could not read jobs: {exc}"}]
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("doctor.json", json.dumps(doctor.report(), indent=1))
        zf.writestr("packages.json", json.dumps(_packages(), indent=1))
        zf.writestr("settings.json", json.dumps(settings_summary(), indent=1))
        zf.writestr("jobs.json", json.dumps(jobs, indent=1, default=str))
        zf.writestr("log-tail.txt", _log_tail())
    target.chmod(0o600)
    return target
