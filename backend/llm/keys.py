"""API keys for model providers, kept in the OS keychain.

Never in `.env`, never in the app database, never in logs: a key read from the
keychain is registered with the log redactor before it is used. The environment
variable the provider's SDK reads (ANTHROPIC_API_KEY) still works as a fallback,
for CI and for people who already manage keys that way.
"""

from __future__ import annotations

import logging
import os

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from backend.core.logsetup import register_secret

log = logging.getLogger(__name__)

SERVICE = "trade-terminal"
ENV_VARS = {"anthropic": "ANTHROPIC_API_KEY"}
PROVIDERS = tuple(ENV_VARS)


def _check(provider: str) -> None:
    if provider not in ENV_VARS:
        raise ValueError(f"Unknown provider {provider!r}. Known: {', '.join(PROVIDERS)}")


def _account(provider: str) -> str:
    return f"{provider}-api-key"


def get_key(provider: str) -> str | None:
    """The keychain key, else the environment variable, else None."""
    _check(provider)
    try:
        key = keyring.get_password(SERVICE, _account(provider))
    except KeyringError as exc:
        log.warning("Keychain unavailable: %s", exc)
        key = None
    key = key or os.environ.get(ENV_VARS[provider]) or None
    if key:
        register_secret(key)
    return key


def key_source(provider: str) -> str | None:
    """Where the key comes from: "keychain", "environment", or None."""
    _check(provider)
    try:
        if keyring.get_password(SERVICE, _account(provider)):
            return "keychain"
    except KeyringError:
        pass
    return "environment" if os.environ.get(ENV_VARS[provider]) else None


def set_key(provider: str, key: str) -> None:
    _check(provider)
    key = key.strip()
    if len(key) < 20 or any(c.isspace() for c in key):
        raise ValueError("That does not look like an API key.")
    register_secret(key)
    keyring.set_password(SERVICE, _account(provider), key)


def delete_key(provider: str) -> bool:
    _check(provider)
    try:
        keyring.delete_password(SERVICE, _account(provider))
        return True
    except PasswordDeleteError:
        return False
