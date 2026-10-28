"""Telegram alerts: text from outside this module is escaped, and failures never raise."""

from __future__ import annotations

import re
from decimal import Decimal

import pytest
from pydantic import SecretStr

from backend.core.config import settings
from backend.live import alerts as A


@pytest.fixture
def sent(monkeypatch):
    """Configure Telegram and capture what would have been posted."""
    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr("123:fake-token-for-tests"))
    monkeypatch.setattr(settings, "telegram_chat_id", "42")
    messages: list[str] = []

    class Response:
        status_code = 200

    def fake_post(url, json, timeout):
        assert json["parse_mode"] == "HTML"
        messages.append(json["text"])
        return Response()

    monkeypatch.setattr(A.httpx, "post", fake_post)
    return messages


def _is_valid_telegram_html(text: str) -> bool:
    """Only our own <b> tags, and every & starts an entity."""
    without_tags = re.sub(r"</?b>", "", text)
    if "<" in without_tags or ">" in without_tags:
        return False
    return all(re.match(r"&(amp|lt|gt|quot);", text[i:]) for i, c in enumerate(text) if c == "&")


def test_strategy_names_with_markup_are_escaped(sent):
    alerts = A.Alerts()
    name = "Buy <ATM> CE if RSI<30 & EMA>VWAP"
    alerts.session_start(name, "NSE_INDEX|Nifty Bank", "5m", "paper")
    alerts.session_end(name, 3, Decimal("-1250.5"), "manual")

    assert len(sent) == 2
    for text in sent:
        assert _is_valid_telegram_html(text), text
    assert "Buy &lt;ATM&gt; CE if RSI&lt;30 &amp; EMA&gt;VWAP" in sent[0]
    assert "<b>PAPER</b>" in sent[0]


def test_exit_alert_is_valid_html(sent):
    A.Alerts().exit("SIM|X", 250, Decimal("101.5"), Decimal("350"), "stop <loss>")
    assert _is_valid_telegram_html(sent[0]), sent[0]
    assert "P&amp;L +350.00" in sent[0]


def test_error_text_is_truncated_then_escaped(sent):
    A.Alerts().error("x" * 599 + "&" + "tail")
    assert _is_valid_telegram_html(sent[0])
    assert sent[0].endswith("x&amp;")


def test_send_failure_never_raises(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr("123:fake-token-for-tests"))
    monkeypatch.setattr(settings, "telegram_chat_id", "42")

    def boom(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(A.httpx, "post", boom)
    alerts = A.Alerts()
    assert alerts.send("hello") is False
    assert alerts.failed == 1
