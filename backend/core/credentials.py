"""Credentials in the OS keychain, with `.env` as an explicit fallback.

    credentials.get("upstox_api_secret")      # keychain first, then .env, else ""

Every value read is registered with the log redactor before it is returned, so
whatever the caller does with it next, it cannot reach a log line in clear.

The keychain wins when both hold a value; if they differ, a warning says so
(`doctor` reports it too), because a stale keychain entry silently overriding a
freshly pasted `.env` value is exactly the kind of thing that wastes a morning.

Machines without a usable keychain (a headless Linux server) fall back to
`.env` for reads; writes there raise `SecretStoreUnavailable` with a message
that says what to do instead.

The Anthropic key keeps its own module (backend/llm/keys.py), which follows the
same rules with the provider's environment variable as its fallback.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from backend.core.config import settings
from backend.core.logsetup import register_secret

log = logging.getLogger(__name__)

SERVICE = "trade-terminal"


@dataclass(frozen=True)
class SecretSpec:
    name: str
    label: str
    env_var: str | None        # the .env setting it falls back to, if any


SPECS: dict[str, SecretSpec] = {
    s.name: s for s in (
        SecretSpec("upstox_api_secret", "Upstox API secret", "UPSTOX_API_SECRET"),
        SecretSpec("upstox_access_token", "Upstox access token (pasted)", "UPSTOX_ACCESS_TOKEN"),
        SecretSpec("upstox_session", "Upstox session from the browser login", None),
        SecretSpec("telegram_bot_token", "Telegram bot token", "TELEGRAM_BOT_TOKEN"),
        SecretSpec("kite_api_secret", "Zerodha Kite API secret", "KITE_API_SECRET"),
    )
}

_warned_conflict: set[str] = set()


class SecretStoreUnavailable(RuntimeError):
    pass


def _spec(name: str) -> SecretSpec:
    if name not in SPECS:
        raise KeyError(f"Unknown secret {name!r}. Known: {', '.join(SPECS)}")
    return SPECS[name]


def _from_keychain(name: str) -> str:
    try:
        return (keyring.get_password(SERVICE, name) or "").strip()
    except KeyringError as exc:
        log.debug("Keychain unavailable for %s: %s", name, exc)
        return ""


def _from_env(name: str) -> str:
    if _spec(name).env_var is None:
        return ""
    value = getattr(settings, name, None)
    return value.get_secret_value().strip() if value is not None else ""


def get(name: str) -> str:
    """The secret's value, or "" when it is not set anywhere."""
    _spec(name)
    stored, env = _from_keychain(name), _from_env(name)
    if stored and env and stored != env and name not in _warned_conflict:
        _warned_conflict.add(name)
        log.warning(
            "%s is set in both the keychain and .env with different values; using the "
            "keychain. Remove one: trade-terminal credentials delete %s, or delete the .env line.",
            _spec(name).label, name,
        )
    value = stored or env
    if value:
        register_secret(value)
    return value


def source(name: str) -> str | None:
    """Where the value comes from: "keychain", ".env", or None."""
    _spec(name)
    if _from_keychain(name):
        return "keychain"
    return ".env" if _from_env(name) else None


def conflicts() -> list[str]:
    """Secrets set in both places with different values."""
    return [
        name for name in SPECS
        if (a := _from_keychain(name)) and (b := _from_env(name)) and a != b
    ]


def in_env_file() -> list[str]:
    """Secrets currently held in plaintext in .env."""
    return [name for name in SPECS if _from_env(name)]


def set(name: str, value: str) -> None:  # noqa: A001 - mirrors keyring's verb
    _spec(name)
    value = value.strip()
    if not value:
        raise ValueError("An empty value cannot be stored; use delete instead.")
    if any(c.isspace() for c in value) and name != "upstox_session":
        raise ValueError("That value contains whitespace; secrets and tokens never do.")
    register_secret(value)
    try:
        keyring.set_password(SERVICE, name, value)
    except KeyringError as exc:
        raise SecretStoreUnavailable(
            f"No usable OS keychain here ({exc}). Put {_spec(name).env_var or name} in the "
            f".env file instead, and keep that file private (chmod 600)."
        ) from exc
    _warned_conflict.discard(name)


def delete(name: str) -> bool:
    _spec(name)
    try:
        keyring.delete_password(SERVICE, name)
        return True
    except PasswordDeleteError:
        return False
    except KeyringError as exc:
        raise SecretStoreUnavailable(f"No usable OS keychain here ({exc}).") from exc


def register_all() -> None:
    """Teach the log redactor every stored secret at startup.

    `get` registers values as they are used; this covers anything that could
    be logged before its first use (an exception quoting a token, say).
    """
    for name in SPECS:
        try:
            get(name)
        except Exception:      # redaction setup must never stop the app
            pass


def status() -> list[dict[str, str | None]]:
    """For display: names, labels and sources. Never values."""
    return [
        {"name": s.name, "label": s.label, "source": source(s.name), "env_var": s.env_var}
        for s in SPECS.values()
    ]


def import_from_env(remove_from_env: bool = False, env_path=None) -> dict[str, list[str]]:
    """Copy secrets held in .env into the keychain.

    Each value is read back from the keychain before anything else happens.
    With `remove_from_env`, the imported lines are then deleted from the .env
    file (rewritten atomically, owner-only); without it, the file is untouched
    and the caller says which lines to delete.
    """
    from backend.core.config import HOME

    imported, skipped = [], []
    for name in in_env_file():
        value = _from_env(name)
        current = _from_keychain(name)
        if current and current != value:
            skipped.append(name)             # never overwrite a different keychain value
            continue
        set(name, value)
        if _from_keychain(name) != value:
            raise SecretStoreUnavailable(f"{name} did not read back from the keychain; nothing removed")
        imported.append(name)

    removed: list[str] = []
    if remove_from_env and imported:
        removed = _strip_env_lines(env_path or (HOME / ".env"), [SPECS[n].env_var for n in imported])
    return {"imported": imported, "skipped": skipped, "removed_from_env": removed}


def _strip_env_lines(path, env_vars: list[str | None]) -> list[str]:
    import os
    import re
    from pathlib import Path

    path = Path(path)
    if not path.exists():
        return []
    wanted = {v for v in env_vars if v}
    kept, removed = [], []
    for line in path.read_text().splitlines(keepends=True):
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match.group(1) in wanted:
            removed.append(match.group(1))
            continue
        kept.append(line)
    partial = path.with_name(path.name + ".tmp")
    partial.write_text("".join(kept))
    os.chmod(partial, 0o600)
    partial.replace(path)
    return removed
