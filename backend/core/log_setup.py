"""One logging setup for the API and every CLI, with secrets redacted.

Call `configure_logging()` once at process start. It honours `LOG_LEVEL`, writes
to the console and (unless told otherwise) to a rotating file under
`data/logs/`, and passes every line through a redacting formatter.

Redaction is not optional. The Telegram bot token is part of the request URL,
httpx logs request URLs at INFO and puts them in its exception messages, and
the Upstox OAuth redirect carries an authorization code. Turning logging on
without redaction would write those to disk.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
import threading

from backend.core.config import insecure_env_file_warning, settings

LOG_FILE_NAME = "trade-terminal.log"
DEFAULT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
MASK = "***"

# Credentials that are only known at runtime (e.g. a freshly exchanged token).
_runtime_secrets: set[str] = set()
_runtime_lock = threading.Lock()

# Shapes of secrets that can appear without us knowing the value in advance.
_PATTERNS = [
    # Telegram: https://api.telegram.org/bot<id>:<token>/sendMessage
    (re.compile(r"(/bot)\d+:[A-Za-z0-9_-]+"), r"\1" + MASK),
    # Authorization headers
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1" + MASK),
    # Query-string or form credentials: ?code=..., &access_token=..., client_secret=...
    (
        re.compile(
            r"(?i)\b(code|access_token|refresh_token|client_secret|api_secret|token)"
            r"(=)[^&\s\"']+"
        ),
        r"\1\2" + MASK,
    ),
    # JSON-ish credentials: "access_token": "..."
    (
        re.compile(r"(?i)(\"(?:access_token|client_secret|api_secret)\"\s*:\s*\")[^\"]+"),
        r"\1" + MASK,
    ),
]

# Secret values shorter than this are not substituted verbatim: masking every
# occurrence of a 3-character string would shred ordinary log lines.
_MIN_SECRET_LEN = 8


def register_secret(value: str | None) -> None:
    """Mask `value` in every log line from now on."""
    if value and len(value) >= _MIN_SECRET_LEN:
        with _runtime_lock:
            _runtime_secrets.add(value)


def _configured_secrets() -> set[str]:
    values = {
        settings.upstox_api_secret.get_secret_value(),
        settings.upstox_access_token.get_secret_value(),
        settings.kite_api_secret.get_secret_value(),
        settings.telegram_bot_token.get_secret_value(),
    }
    with _runtime_lock:
        values |= _runtime_secrets
    return {v.strip() for v in values if v and len(v.strip()) >= _MIN_SECRET_LEN}


def redact(text: str) -> str:
    # Longest first, so a secret containing another secret is masked whole.
    for secret in sorted(_configured_secrets(), key=len, reverse=True):
        text = text.replace(secret, MASK)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Formats normally, then masks secrets, so tracebacks are covered too."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class _OurHandler:
    """Marker so a second configure_logging() call replaces, not duplicates, handlers."""


class _StreamHandler(logging.StreamHandler, _OurHandler):
    pass


class _FileHandler(logging.handlers.RotatingFileHandler, _OurHandler):
    pass


def configure_logging(
    *,
    level: str | int | None = None,
    console_format: str = DEFAULT_FORMAT,
    datefmt: str | None = "%H:%M:%S",
    stream=None,
    to_file: bool = True,
) -> None:
    """Set up root logging for this process. Safe to call more than once."""
    resolved = level if level is not None else settings.log_level
    if isinstance(resolved, str):
        resolved = getattr(logging, resolved.upper(), logging.INFO)

    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, _OurHandler):
            root.removeHandler(handler)
            handler.close()
    root.setLevel(resolved)

    console = _StreamHandler(stream or sys.stderr)
    console.setFormatter(RedactingFormatter(console_format, datefmt=datefmt))
    root.addHandler(console)

    if to_file:
        log_dir = settings.data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = _FileHandler(
            log_dir / LOG_FILE_NAME, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(
            RedactingFormatter(DEFAULT_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
        )
        root.addHandler(file_handler)

    # httpx logs every request URL at INFO: noisy, and the URL can carry a token.
    # Redaction covers the token; this covers the noise.
    if resolved > logging.DEBUG:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

    # Every process starts here, so this is where start-up hygiene is reported.
    if warning := insecure_env_file_warning():
        logging.getLogger("backend.core.config").warning(warning)
