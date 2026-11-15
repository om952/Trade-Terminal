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

# A source checkout keeps its data and .env next to the code. An installed copy
# (pipx, uv tool) has no such place: ROOT is site-packages, which must never be
# written to and is replaced on upgrade. It uses a per-user folder instead.
# TRADE_TERMINAL_HOME overrides either.
IS_CHECKOUT = (ROOT / "pyproject.toml").exists()


def _home() -> Path:
    override = os.environ.get("TRADE_TERMINAL_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    if IS_CHECKOUT:
        return ROOT
    import platformdirs

    return Path(platformdirs.user_data_dir("trade-terminal", appauthor=False))


HOME = _home()
IST = ZoneInfo("Asia/Kolkata")


class Mode(str, Enum):
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=HOME / ".env",
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

    # --- Strategy builder (LLM) ---
    # Keys live in the OS keychain (Settings in the UI), not here.
    llm_provider: str = "anthropic"
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "high"
    # Only for LLM_PROVIDER=scripted (end-to-end tests, offline demos).
    llm_script: Path | None = None
    # Hard caps per compile, enforced before every model call.
    llm_max_tokens_per_compile: int = 200_000
    llm_max_steps: int = 8
    llm_max_seconds: float = 300.0

    # --- Local API ---
    # Extra hostnames the API will answer to, comma-separated. Loopback names
    # are always allowed; anything else is refused to block DNS rebinding.
    # Leave empty unless the API is deliberately reached under another name.
    api_allowed_hosts: str = ""
    # Extra browser origins allowed to call the API (comma-separated), beyond
    # the Vite dev server and the API's own origin. For test UIs on other ports.
    api_allowed_origins: str = ""

    # --- Paths ---
    data_dir: Path = Field(default=HOME / "data")

    @property
    def parquet_dir(self) -> Path:
        return self.data_dir / "parquet"

    @property
    def journal_dir(self) -> Path:
        return self.data_dir / "journal"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def app_db_path(self) -> Path:
        """App state: strategies, versions, runs, jobs (SQLite)."""
        return self.data_dir / "app.sqlite"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "terminal.duckdb"

    @property
    def token_path(self) -> Path:
        """Cached access token. Gitignored — it is a bearer credential."""
        return self.data_dir / ".upstox_token.json"

    def require_upstox(self) -> None:
        """Fail loudly and early rather than at the first API call."""
        from backend.core import credentials

        if not self.upstox_api_key or not credentials.get("upstox_api_secret"):
            raise RuntimeError(
                "UPSTOX_API_KEY (in .env) or the Upstox API secret is missing. Store the "
                "secret with: trade-terminal credentials set upstox_api_secret. Create an app "
                "at https://account.upstox.com/developer/apps"
            )

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.parquet_dir, self.journal_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
