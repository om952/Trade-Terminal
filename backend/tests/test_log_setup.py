"""Logging: one setup for every process, and no secret ever reaches a log line."""

from __future__ import annotations

import io
import logging

import pytest
from pydantic import SecretStr

from backend.core import log_setup
from backend.core.config import settings


@pytest.fixture
def capture(monkeypatch):
    """Configure logging into a buffer; remove our handlers afterwards."""
    stream = io.StringIO()
    log_setup.configure_logging(level="INFO", stream=stream, to_file=False)
    yield stream
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, log_setup._OurHandler):
            root.removeHandler(handler)
            handler.close()


def test_telegram_token_in_url_is_masked(capture):
    logging.getLogger("t").info(
        "POST https://api.telegram.org/bot123456:AAH-secret_Token/sendMessage failed"
    )
    out = capture.getvalue()
    assert "AAH-secret_Token" not in out
    assert "/bot***/sendMessage" in out


def test_bearer_and_query_credentials_are_masked(capture):
    log = logging.getLogger("t")
    log.info("headers={'Authorization': 'Bearer eyJhbGciOi.payload.sig'}")
    log.info("redirect https://127.0.0.1:8000/cb?code=abc123XYZ&state=s1")
    log.info('body {"access_token": "tok-value-1234"}')
    out = capture.getvalue()
    for leaked in ("eyJhbGciOi.payload.sig", "abc123XYZ", "tok-value-1234"):
        assert leaked not in out
    assert "state=s1" in out  # non-secret parameters survive


def test_configured_secret_values_are_masked(capture, monkeypatch):
    monkeypatch.setattr(settings, "upstox_api_secret", SecretStr("my-upstox-secret-value"))
    logging.getLogger("t").warning("oops, secret was my-upstox-secret-value")
    assert "my-upstox-secret-value" not in capture.getvalue()


def test_runtime_registered_secret_is_masked(capture):
    log_setup.register_secret("runtime-token-0987654321")
    logging.getLogger("t").info("got runtime-token-0987654321 from broker")
    assert "runtime-token-0987654321" not in capture.getvalue()


def test_tracebacks_are_masked(capture):
    try:
        raise RuntimeError("request to /bot42:LEAKY_TOKEN_VALUE/sendMessage failed")
    except RuntimeError:
        logging.getLogger("t").exception("send failed")
    out = capture.getvalue()
    assert "LEAKY_TOKEN_VALUE" not in out
    assert "Traceback" in out


def test_configure_twice_does_not_duplicate_lines(capture):
    stream = io.StringIO()
    log_setup.configure_logging(level="INFO", stream=stream, to_file=False)
    log_setup.configure_logging(level="INFO", stream=stream, to_file=False)
    logging.getLogger("t").info("exactly once")
    assert stream.getvalue().count("exactly once") == 1


def test_file_handler_writes_under_data_dir(capture):
    log_setup.configure_logging(level="INFO", stream=io.StringIO(), to_file=True)
    logging.getLogger("t").info("to the file, token=SHOULD_NOT_APPEAR")
    for handler in logging.getLogger().handlers:
        handler.flush()
    text = (settings.data_dir / "logs" / log_setup.LOG_FILE_NAME).read_text()
    assert "to the file" in text
    assert "SHOULD_NOT_APPEAR" not in text


def test_log_level_setting_is_honoured(monkeypatch):
    monkeypatch.setattr(settings, "log_level", "WARNING")
    log_setup.configure_logging(stream=io.StringIO(), to_file=False)
    try:
        assert logging.getLogger().level == logging.WARNING
    finally:
        log_setup.configure_logging(level="WARNING", stream=io.StringIO(), to_file=False)
        root = logging.getLogger()
        for handler in list(root.handlers):
            if isinstance(handler, log_setup._OurHandler):
                root.removeHandler(handler)
                handler.close()
