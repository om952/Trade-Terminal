"""Phase 0 gate: the platform around the engine.

Logging and redaction, the local-only API guard, clean shutdown, Telegram
escaping and version agreement. None of this is trading logic, and all of it is
what turns a later failure from a mystery into a log line.
"""

from __future__ import annotations

import json
import logging
import tomllib
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from backend.api import security
from backend.api.main import app
from backend.core import logsetup
from backend.core.config import ROOT, settings
from backend.core.version import __version__
from backend.live.alerts import Alerts

TELEGRAM_TOKEN = "123456789:AAH-fake_fake_fake_fake_fake_fake_fake1"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlX2dvZXNfaGVyZQ"


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "line, secret",
    [
        (f"HTTP Request: POST https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", TELEGRAM_TOKEN),
        (f"headers={{'Authorization': 'Bearer {JWT}'}}", JWT),
        (f"token was {JWT} when it failed", JWT),
        ("GET /callback?code=abc123XYZsecret&state=ok", "abc123XYZsecret"),
        ("POST body client_secret=s3cr3t-value-here&grant_type=x", "s3cr3t-value-here"),
        ('response {"access_token": "plain-opaque-token-value", "x": 1}', "plain-opaque-token-value"),
        ("{'api_secret': 'another-opaque-value'}", "another-opaque-value"),
    ],
)
def test_redact_removes_credentials(line, secret):
    cleaned = logsetup.redact(line, secrets=[])
    assert secret not in cleaned
    assert logsetup.REDACTED in cleaned


def test_redact_keeps_the_rest_of_the_line():
    line = "GET /callback?code=abc123XYZsecret&state=ok"
    assert logsetup.redact(line, secrets=[]) == f"GET /callback?code={logsetup.REDACTED}&state=ok"


def test_redact_leaves_ordinary_lines_alone():
    line = "Session 20260101-NIFTY-abc started: 400 warm-up bars, token valid for 6.5h"
    assert logsetup.redact(line, secrets=[]) == line


def test_redact_removes_configured_secret_values():
    line = "upstream said: invalid credential opaque-configured-secret"
    assert "opaque-configured-secret" not in logsetup.redact(
        line, secrets=["opaque-configured-secret"]
    )


@pytest.fixture
def configured_logging(tmp_path, monkeypatch):
    """Real handlers, pointed at a temp dir, removed afterwards."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    root = logging.getLogger()
    level = root.level
    logsetup.configure_logging("INFO")
    yield tmp_path / "logs" / logsetup.LOG_FILE_NAME
    logsetup.reset_logging()
    root.setLevel(level)


def _flush() -> None:
    for handler in logging.getLogger().handlers:
        handler.flush()


def test_log_file_never_contains_a_secret(configured_logging):
    """Message, arguments and traceback all pass through redaction."""
    log = logging.getLogger("backend.test.redaction")
    log.info("POST https://api.telegram.org/bot%s/sendMessage", TELEGRAM_TOKEN)
    try:
        raise RuntimeError(f"401 for Authorization: Bearer {JWT}")
    except RuntimeError:
        log.exception("Upstox call failed")
    _flush()

    text = configured_logging.read_text()
    assert "Upstox call failed" in text and "sendMessage" in text
    assert TELEGRAM_TOKEN not in text
    assert JWT not in text


def test_application_info_lines_reach_the_log_file(configured_logging):
    logging.getLogger("backend.live.session").info("paper session line")
    _flush()
    assert "paper session line" in configured_logging.read_text()


def test_log_file_is_owner_only(configured_logging):
    assert configured_logging.stat().st_mode & 0o777 == 0o600


def test_configure_logging_twice_does_not_duplicate_lines(configured_logging):
    logsetup.configure_logging("INFO")
    logging.getLogger("backend.test.dupes").info("exactly once")
    _flush()
    assert configured_logging.read_text().count("exactly once") == 1


def test_log_level_is_honoured(configured_logging):
    logsetup.configure_logging("WARNING")
    log = logging.getLogger("backend.test.level")
    log.info("should be dropped")
    log.warning("should be kept")
    _flush()
    text = configured_logging.read_text()
    assert "should be kept" in text and "should be dropped" not in text


def test_http_client_loggers_are_quietened(configured_logging):
    """httpx logs every request URL at INFO, and the Telegram URL holds the token."""
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


# ---------------------------------------------------------------------------
# Local-only API guard
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "host, allowed",
    [
        ("127.0.0.1:8000", True),
        ("localhost:8000", True),
        ("LOCALHOST", True),
        ("[::1]:8000", True),
        ("evil.example.com", False),
        ("evil.example.com:8000", False),
        ("127.0.0.1.evil.example.com", False),
        ("localhost.evil.example.com:8000", False),
        ("192.168.1.20:8000", False),
        ("", False),
        (None, False),
    ],
)
def test_host_allowed(host, allowed):
    assert security.host_allowed(host) is allowed


def test_extra_hosts_are_opt_in():
    extra = security.parse_extra_hosts(" Terminal.Local , ")
    assert extra == {"terminal.local"}
    assert security.host_allowed("terminal.local:8000", extra)
    assert not security.host_allowed("terminal.local:8000")


@pytest.mark.parametrize(
    "origin, allowed",
    [
        (None, True),                           # curl, CLI: no browser involved
        ("http://localhost:5173", True),        # Vite dev UI
        ("http://127.0.0.1:5173", True),
        ("http://127.0.0.1:8000", True),        # UI served by the API itself
        ("http://localhost:9999", False),       # some other local web app
        ("https://evil.example.com", False),
        ("null", False),                        # sandboxed iframe, file://
        ("", False),
    ],
)
def test_origin_allowed(origin, allowed):
    assert security.origin_allowed(origin, "127.0.0.1:8000") is allowed


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1:8000")


def test_api_refuses_a_rebound_hostname(client):
    """DNS rebinding: the request reaches loopback but names the attacker's host."""
    response = client.get("/api/health", headers={"Host": "evil.example.com"})
    assert response.status_code == 403
    assert "Host" in response.json()["detail"]


def test_api_refuses_a_cross_site_post(client):
    """A page on another origin must not be able to start a session."""
    response = client.post(
        "/api/live/start", json={"strategy": {}}, headers={"Origin": "https://evil.example.com"}
    )
    assert response.status_code == 403
    assert "Origin" in response.json()["detail"]


def test_api_accepts_the_dev_ui_and_plain_clients(client):
    assert client.get("/api/health").status_code == 200
    from_ui = client.get("/api/health", headers={"Origin": "http://localhost:5173"})
    assert from_ui.status_code == 200
    assert from_ui.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_refusal_is_readable_from_the_dev_ui(client):
    """CORS wraps the guard, so the UI sees the 403 body rather than a network error."""
    response = client.get(
        "/api/health", headers={"Host": "evil.example.com", "Origin": "http://localhost:5173"}
    )
    assert response.status_code == 403
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def test_shutdown_stops_running_sessions_and_closes_their_journals():
    """Exiting the API must leave every journal ended, not looking like a crash."""
    from backend.data.simulator import generate_ohlcv
    from backend.data.store import write_candles
    from backend.live import journal as J
    from backend.live.manager import manager
    from backend.strategy import templates

    key = "SIM|LIFESPAN"
    frame = generate_ohlcv("RELIANCE", date(2025, 1, 1), date(2025, 6, 30), "5m", seed=7)
    write_candles(key, "5m", frame, source="simulated")
    strategy = templates.load("ema_crossover", symbol=key).model_dump(mode="json")
    strategy["timeframe"] = "5m"

    root = logging.getLogger()
    level = root.level
    try:
        with TestClient(app, base_url="http://127.0.0.1:8000") as live:
            started = live.post(
                "/api/live/start",
                json={
                    "strategy": strategy, "symbol": key, "source": "replay",
                    # Real-time pacing: the session is asleep between bars for
                    # minutes at a time, so only an interruptible stop ends it.
                    "replay_start": "2025-03-01", "replay_speed": 1, "alerts": False,
                },
            )
            assert started.status_code == 200, started.text
            session_id = started.json()["session_id"]
            assert manager.is_running(session_id)
    finally:
        logsetup.reset_logging()
        root.setLevel(level)

    assert not manager.is_running(session_id)
    assert J.recover(session_id).ended, "journal has no end record after shutdown"
    ends = [
        r.data for r in J.read(settings.journal_dir / f"{session_id}.jsonl")
        if r.type is J.Event.SESSION_END
    ]
    # "shutdown", not "feed_exhausted": the replay was cut short, it did not finish.
    assert [e["reason"] for e in ends] == ["shutdown"]


# ---------------------------------------------------------------------------
# Telegram escaping
# ---------------------------------------------------------------------------

class _CapturingAlerts(Alerts):
    def __init__(self) -> None:
        super().__init__(enabled=False)
        self.messages: list[str] = []

    def send(self, text: str) -> bool:
        self.messages.append(text)
        return True


def test_alert_text_is_html_escaped():
    alerts = _CapturingAlerts()
    alerts.session_start("RSI<30 & <b>breakout", "NSE_INDEX|Nifty 50", "5m", "paper")
    alerts.error("quantity < 0 for leg <main> & co")
    alerts.blocked("max_trades", "3 >= limit <3>")
    alerts.session_end("A<B", 2, Decimal("-10.5"), "manual")
    alerts.exit("X<Y", 1, Decimal("100"), Decimal("5"), "stop<loss")

    start, error, blocked, end, exit_ = alerts.messages
    assert "RSI&lt;30 &amp; &lt;b&gt;breakout" in start
    assert "<b>PAPER</b>" in start, "the template's own markup must survive"
    assert "quantity &lt; 0 for leg &lt;main&gt; &amp; co" in error
    assert "3 &gt;= limit &lt;3&gt;" in blocked
    assert "A&lt;B" in end
    assert "X&lt;Y" in exit_ and "stop&lt;loss" in exit_ and "P&amp;L" in exit_


def test_alert_error_truncation_cannot_split_an_entity():
    alerts = _CapturingAlerts()
    alerts.error("x" * 599 + "<" + "tail")
    assert alerts.messages[0].endswith("&lt;")


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

def test_versions_agree():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    frontend = json.loads((ROOT / "frontend" / "package.json").read_text())["version"]
    assert pyproject == frontend == __version__ == app.version
