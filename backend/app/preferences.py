"""User preferences, stored per user in the app database.

Only keys listed in DEFAULTS exist: an unknown key is a typo, and a typo in a
preference name would otherwise read back as "not set" forever.
"""

from __future__ import annotations

import json
from typing import Any

from backend.app.context import AppContext
from backend.app.db import get_app_db, now_iso

DEFAULTS: dict[str, Any] = {
    # What happens to the session's downloaded market data at quit:
    # "ask" every time, "keep" it in the local library, or "discard" it.
    "data_retention": "ask",
    # An app session ends after this long with no UI connected and no paper session.
    "idle_minutes": 30,
    # How long a paused conversation stays resumable exactly where it stopped.
    "checkpoint_retention_days": 15,
    # Download only 1-minute bars and build 3m, 5m, 15m, 1h... locally, instead
    # of downloading each timeframe. Off until `ingest verify-derivation` shows
    # derived bars match the broker's native ones on your data: paper trading
    # reads native bars, and backtests must see the same ones.
    "derive_intraday_from_1m": False,
}

ALLOWED_VALUES: dict[str, set[Any]] = {
    "data_retention": {"ask", "keep", "discard"},
    "derive_intraday_from_1m": {True, False},
}


def _check(key: str, value: Any = None, *, check_value: bool = False) -> None:
    if key not in DEFAULTS:
        raise KeyError(f"Unknown preference {key!r}. Known: {sorted(DEFAULTS)}")
    if check_value and key in ALLOWED_VALUES and value not in ALLOWED_VALUES[key]:
        raise ValueError(f"{key} must be one of {sorted(ALLOWED_VALUES[key])}, got {value!r}")


def get(key: str, ctx: AppContext | None = None) -> Any:
    _check(key)
    ctx = ctx or AppContext.local()
    with get_app_db(ctx).read() as conn:
        row = conn.execute(
            "SELECT value_json FROM preferences WHERE user_id = ? AND key = ?",
            (ctx.user_id, key),
        ).fetchone()
    return json.loads(row["value_json"]) if row else DEFAULTS[key]


def set(key: str, value: Any, ctx: AppContext | None = None) -> None:  # noqa: A001
    _check(key, value, check_value=True)
    ctx = ctx or AppContext.local()
    with get_app_db(ctx).connect() as conn:
        conn.execute(
            "INSERT INTO preferences (user_id, key, value_json, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (user_id, key) DO UPDATE SET value_json = excluded.value_json, "
            "updated_at = excluded.updated_at",
            (ctx.user_id, key, json.dumps(value), now_iso()),
        )


def all_preferences(ctx: AppContext | None = None) -> dict[str, Any]:
    return {key: get(key, ctx) for key in DEFAULTS}
