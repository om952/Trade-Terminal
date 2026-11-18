"""One logging setup for every entry point: the API, the live CLI and the ingest CLI.

Before this module only the CLIs configured logging, so under `uvicorn` every
application `INFO` line, including the paper-session ones, was dropped.

Redaction is not optional. The Telegram bot token sits in the request URL, and
`httpx` logs request URLs at INFO; an Upstox bearer token appears in headers and
can surface in an exception message. Every handler installed here formats
through `RedactingFormatter`, which scrubs the finished line (message, arguments
and traceback alike), so a secret cannot reach the console or the log file by
any route that goes through logging.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from typing import TextIO

from backend.core.config import settings

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
LOG_FILE_NAME = "terminal.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5
REDACTED = "<redacted>"

# Chatty at INFO (one line per HTTP request, which a 5-second poll turns into
# noise) and the main route by which a URL-embedded token would be logged.
_QUIET_LOGGERS = ("httpx", "httpcore")

# uvicorn's loggers do not propagate to the root, so they need the file handler
# attached directly to appear in the log file.
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.access")

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Telegram bot token inside an API URL: /bot123456789:AA...
    (re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{20,}"), f"bot{REDACTED}"),
    # Authorization header values.
    (re.compile(r"(?i)\b(bearer|token)\s+[A-Za-z0-9._~+/=-]{12,}"), rf"\1 {REDACTED}"),
    # JSON Web Tokens, wherever they appear (Upstox access tokens are JWTs).
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), REDACTED),
    # key=value in query strings and form bodies.
    (
        re.compile(
            r"(?i)\b(access_token|refresh_token|client_secret|api_secret|api_key|apikey"
            r"|secret|password|code)=([^&\s\"']+)"
        ),
        rf"\1={REDACTED}",
    ),
    # "key": "value" in JSON and dict reprs.
    (
        re.compile(
            r"(?i)([\"'](?:access_token|refresh_token|client_secret|api_secret|api_key"
            r"|secret|password)[\"']\s*:\s*[\"'])[^\"']+([\"'])"
        ),
        rf"\1{REDACTED}\2",
    ),
)

# Shorter values are too likely to collide with ordinary text.
_MIN_LITERAL_LENGTH = 8

# Secrets that do not live in settings (LLM keys read from the OS keychain).
_registered: set[str] = set()


def register_secret(value: str) -> None:
    """Redact `value` from every log line from now on."""
    value = value.strip()
    if len(value) >= _MIN_LITERAL_LENGTH:
        _registered.add(value)


def _configured_secrets() -> list[str]:
    """Secret values from settings, so they are caught even outside a known pattern."""
    candidates = (
        settings.upstox_api_secret,
        settings.upstox_access_token,
        settings.kite_api_secret,
        settings.telegram_bot_token,
    )
    values = {c.get_secret_value().strip() for c in candidates} | _registered
    # Longest first, so a secret that contains another is replaced whole.
    return sorted((v for v in values if len(v) >= _MIN_LITERAL_LENGTH), key=len, reverse=True)


def redact(text: str, secrets: list[str] | None = None) -> str:
    """Remove credentials from a line of text."""
    for secret in _configured_secrets() if secrets is None else secrets:
        text = text.replace(secret, REDACTED)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Formats normally, then scrubs the result.

    Working on the finished string rather than on `record.msg` covers the
    arguments and the traceback as well as the message.
    """

    def __init__(self, fmt: str = LOG_FORMAT, datefmt: str | None = None) -> None:
        super().__init__(fmt, datefmt)
        self._secrets = _configured_secrets()
        self._registered_seen = len(_registered)

    def format(self, record: logging.LogRecord) -> str:
        if len(_registered) != self._registered_seen:     # a key was added since
            self._secrets = _configured_secrets()
            self._registered_seen = len(_registered)
        return redact(super().format(record), self._secrets)


_installed: list[tuple[logging.Logger, logging.Handler]] = []


def _resolve_level(level: str | int | None) -> int:
    if isinstance(level, int):
        return level
    name = (level or settings.log_level or "INFO").upper()
    resolved = logging.getLevelName(name)
    return resolved if isinstance(resolved, int) else logging.INFO


def configure_logging(
    level: str | int | None = None,
    *,
    stream: TextIO | None = None,
    to_file: bool = True,
) -> None:
    """Install console and rotating-file handlers on the root logger.

    Safe to call more than once: handlers from an earlier call are replaced, not
    stacked, so a reload does not double every line. `level` defaults to
    `LOG_LEVEL` from settings.
    """
    reset_logging()

    resolved = _resolve_level(level)
    root = logging.getLogger()
    root.setLevel(resolved)

    console = logging.StreamHandler(stream or sys.stderr)
    console.setFormatter(RedactingFormatter(datefmt="%H:%M:%S"))
    _install(root, console)

    if to_file:
        try:
            settings.logs_dir.mkdir(parents=True, exist_ok=True)
            path = settings.logs_dir / LOG_FILE_NAME
            file_handler = RotatingFileHandler(
                path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
            )
            # Redacted, but still a record of account activity: owner-only.
            os.chmod(path, 0o600)
        except OSError as exc:
            root.warning("File logging disabled, cannot write under %s: %s", settings.logs_dir, exc)
        else:
            file_handler.setFormatter(RedactingFormatter())
            _install(root, file_handler)
            for name in _UVICORN_LOGGERS:
                _install(logging.getLogger(name), file_handler)

    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(max(resolved, logging.WARNING))

    # Keychain-held secrets are not in settings; teach the redactor them now,
    # before anything can log one.
    from backend.core import credentials

    credentials.register_all()


def _install(logger: logging.Logger, handler: logging.Handler) -> None:
    logger.addHandler(handler)
    _installed.append((logger, handler))


def reset_logging() -> None:
    """Remove every handler this module installed."""
    closed: set[int] = set()
    for logger, handler in _installed:
        logger.removeHandler(handler)
        if id(handler) not in closed:
            handler.close()
            closed.add(id(handler))
    _installed.clear()
