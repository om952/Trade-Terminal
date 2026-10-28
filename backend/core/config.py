"""Configuration loaded from .env. Secrets never get logged or serialized.

Import `settings` from here; do not read os.environ anywhere else in the codebase.
That way there is exactly one place to look when something is misconfigured.
"""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
IST = ZoneInfo("Asia/Kolkata")

# Which .env to read. Unset means the repo's .env; an empty string means none at
# all, which is how the test suite keeps a developer's real secrets and risk
# limits out of its runs.
_env_override = os.environ.get("TRADE_TERMINAL_ENV_FILE")
ENV_FILE: Path | None = (
    ROOT / ".env" if _env_override is None else (Path(_env_override) if _env_override else None)
)


class Mode(str, Enum):
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Upstox ---
    upstox_api_key: str = ""
    upstox_api_secret: SecretStr = SecretStr("")
    upstox_redirect_uri: str = "https://127.0.0.1:8000/auth/upstox/callback"

    # Optional: paste a token straight into .env instead of running the browser
    # flow. Takes precedence over the cached token file when set. Convenient
    # when you already have today's token from elsewhere.
    upstox_access_token: SecretStr = SecretStr("")

    # --- Zerodha (Phase 8) ---
    kite_api_key: str = ""
    kite_api_secret: SecretStr = SecretStr("")

    # --- Alerts ---
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""

    # --- Runtime ---
    mode: Mode = Mode.BACKTEST
    log_level: str = "INFO"

    # --- Risk: account-level kill switch, enforced by the engine.
    # No strategy document can raise these. Empty means "not configured",
    # which the engine treats as a hard block on live mode.
    max_daily_loss: float | None = None
    max_concurrent_positions: int | None = None

    @field_validator("max_daily_loss", "max_concurrent_positions", mode="before")
    @classmethod
    def _blank_is_none(cls, v: object) -> object:
        """A key present but unset in .env means "not configured", not zero.

        Zero would be a legitimate — and catastrophic — value here, so it must
        never be produced by accident.
        """
        return None if isinstance(v, str) and not v.strip() else v

    # --- Paths ---
    data_dir: Path = Field(default=ROOT / "data")

    @property
    def parquet_dir(self) -> Path:
        """Where candle files lived before the partitioned store (read only by the
        one-time migration in backend.data.store)."""
        return self.data_dir / "parquet"

    @property
    def journal_dir(self) -> Path:
        return self.data_dir / "journal"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "terminal.duckdb"

    @property
    def token_path(self) -> Path:
        """Cached access token. Gitignored — it is a bearer credential."""
        return self.data_dir / ".upstox_token.json"

    def require_upstox(self) -> None:
        """Fail loudly and early rather than at the first API call."""
        if not self.upstox_api_key or not self.upstox_api_secret.get_secret_value():
            raise RuntimeError(
                "UPSTOX_API_KEY / UPSTOX_API_SECRET missing from .env. "
                "Create an app at https://account.upstox.com/developer/apps"
            )

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.journal_dir):
            d.mkdir(parents=True, exist_ok=True)


def insecure_env_file_warning(env_file: Path | None = None) -> str | None:
    """A warning if the .env file (broker credentials) is readable by other users.

    Reported, not fixed: silently changing permissions on a user's file is worse
    than telling them the one command that fixes it.
    """
    path = env_file if env_file is not None else ENV_FILE
    if path is None or os.name != "posix" or not path.exists():
        return None
    mode = path.stat().st_mode & 0o777
    if not mode & 0o077:
        return None
    return (
        f"{path} is readable by other users (mode {mode:o}) and holds broker "
        f"credentials. Fix it with: chmod 600 {path}"
    )


settings = Settings()
settings.ensure_dirs()
