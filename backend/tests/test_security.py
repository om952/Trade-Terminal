"""P2.5 security checklist: the properties that must hold everywhere, checked together.

Each test here guards a class of leak rather than one function, so a new route,
file or log line that breaks the rule fails here even if its own tests pass.
"""

from __future__ import annotations

import logging
import re
import subprocess
from datetime import date
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import SecretStr

from backend.core import credentials, logsetup
from backend.core.config import ROOT, settings

PLANTED = {
    "upstox_api_secret": "planted-upstox-secret-AAAAAAAAAAAA",
    "telegram_bot_token": "123456789:AAH-planted_planted_planted_planted",
    "upstox_access_token": "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjF9.cGxhbnRlZF9zaWduYXR1cmU",
}
ANTHROPIC_KEY = "sk-ant-api03-" + "P" * 40


@pytest.fixture
def planted(tmp_path, monkeypatch):
    """A data dir with real files in it and every secret set somewhere."""
    from backend.appstate import db
    from backend.data import store
    from backend.data.simulator import generate_ohlcv
    from backend.llm import keys

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store.close_db()
    credentials.set("upstox_api_secret", PLANTED["upstox_api_secret"])
    credentials.set("upstox_access_token", PLANTED["upstox_access_token"])
    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr(PLANTED["telegram_bot_token"]))
    keys.set_key("anthropic", ANTHROPIC_KEY)
    store.write_candles("SIM|SEC", "15m", generate_ohlcv("NIFTY", date(2025, 1, 1), date(2025, 3, 31),
                                                         "15m", seed=1), source="simulated")
    yield tmp_path
    for name in credentials.SPECS:
        credentials.delete(name)
    keys.delete_key("anthropic")
    db.close()
    store.close_db()


def _secrets() -> list[str]:
    return [*PLANTED.values(), ANTHROPIC_KEY]


def test_files_holding_user_state_are_owner_only(planted):
    """Every file the app writes that holds work, credentials or activity."""
    from backend.api.main import app
    from backend.ops import backup, diagnostics
    from backend.strategy import templates

    root, level = logging.getLogger(), logging.getLogger().level
    try:
        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            doc = templates.load("ema_crossover", symbol="SIM|SEC").model_dump(mode="json")
            doc["timeframe"] = "15m"
            client.post("/api/strategies", json={"document": doc})
            client.post("/api/backtest", json={"strategy": doc, "include_chart": False})
            from backend.agent import service

            service.graph()                                    # creates the checkpoint db
        backup.create()
        diagnostics.create()
    finally:
        logsetup.reset_logging()
        root.setLevel(level)

    expected = [
        planted / "app.sqlite",
        planted / "agent_checkpoints.sqlite",
        planted / "logs" / "terminal.log",
        *planted.glob("backups/*.zip"),
        *planted.glob("diagnostics/*.zip"),
    ]
    assert len(expected) >= 5
    loose = {str(p.relative_to(planted)): oct(p.stat().st_mode & 0o777)
             for p in expected if p.stat().st_mode & 0o077}
    assert not loose, f"readable by other users: {loose}"


def test_no_api_response_contains_a_secret(planted):
    from backend.api.main import app

    client = TestClient(app, base_url="http://127.0.0.1:8000")
    for path in ("/api/status", "/api/doctor", "/api/credentials", "/api/llm/status",
                 "/api/health", "/api/datasets", "/api/jobs", "/api/runs"):
        body = client.get(path).text
        for secret in _secrets():
            assert secret not in body, f"{path} leaked a secret"


def test_no_log_line_contains_a_secret(planted, monkeypatch):
    """Through every route a secret could take: arguments, messages, tracebacks, httpx."""
    root, level = logging.getLogger(), logging.getLogger().level
    try:
        logsetup.configure_logging("DEBUG")
        log = logging.getLogger("backend.test.security")
        for secret in _secrets():
            log.info("value %s", secret)
            log.info(f"inline {secret}")
            try:
                raise RuntimeError(f"failed with {secret}")
            except RuntimeError:
                log.exception("caught")
        logging.getLogger("httpx").warning(
            "POST https://api.telegram.org/bot%s/sendMessage", PLANTED["telegram_bot_token"])
        for handler in root.handlers:
            handler.flush()
    finally:
        logsetup.reset_logging()
        root.setLevel(level)
    text = (planted / "logs" / "terminal.log").read_text()
    assert "inline" in text and "caught" in text
    for secret in _secrets():
        assert secret not in text


def test_every_state_changing_route_refuses_a_foreign_origin():
    """A new POST/PUT/DELETE route is protected without anyone remembering to."""
    from backend.api.main import app

    client = TestClient(app, base_url="http://127.0.0.1:8000")
    routes = [r for r in app.routes if isinstance(r, APIRoute)
              and r.methods & {"POST", "PUT", "DELETE"}]
    assert len(routes) >= 15
    for route in routes:
        path = re.sub(r"\{[^}]+\}", "x", route.path)
        for method in route.methods & {"POST", "PUT", "DELETE"}:
            response = client.request(method, path, json={},
                                      headers={"Origin": "https://evil.example.com"})
            assert response.status_code == 403, f"{method} {route.path} accepted a foreign origin"
            response = client.request(method, path, json={}, headers={"Host": "evil.example.com"})
            assert response.status_code == 403, f"{method} {route.path} accepted a foreign host"


def test_secrets_are_never_committed():
    ignored = (ROOT / ".gitignore").read_text()
    for pattern in (".env", "data/journal/", "*.sqlite", "*.duckdb"):
        assert pattern in ignored
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    assert ".env" not in tracked
    assert not any(Path(p).suffix in (".sqlite", ".duckdb", ".pem", ".key") for p in tracked)


def test_example_settings_hold_no_values():
    for line in (ROOT / ".env.example").read_text().splitlines():
        match = re.match(r"\s*([A-Z_]+)\s*=\s*(.*)", line)
        if match and re.search(r"_(SECRET|TOKEN|KEY)$", match.group(1)):
            value = match.group(2).split("#")[0].strip()
            assert value == "", f"{match.group(1)} has a value in .env.example"
