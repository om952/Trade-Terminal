"""P2.5: broker and alert credentials in the OS keychain, .env as the fallback."""

from __future__ import annotations

import io
import json
import logging
from datetime import datetime, timedelta

import keyring
import pytest
from pydantic import SecretStr

from backend import cli
from backend.core import credentials, logsetup
from backend.core.config import IST, settings

SECRET = "upstox-secret-" + "s" * 24
TOKEN = "eyJhbGciOiJIUzI1NiJ9." + "e30" + ".c2lnbmF0dXJlX2dvZXNfaGVyZQ"


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    from backend.data import store

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    for name in credentials.SPECS:
        credentials.delete(name)
        if credentials.SPECS[name].env_var:
            monkeypatch.setattr(settings, name, SecretStr(""))
    credentials._warned_conflict.clear()
    store.close_db()
    yield tmp_path
    for name in credentials.SPECS:
        credentials.delete(name)
    store.close_db()


def _jwt(expires: datetime) -> str:
    import base64

    claims = base64.urlsafe_b64encode(json.dumps({"exp": int(expires.timestamp())}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{claims}.c2lnbmF0dXJl"


def test_keychain_first_then_env(monkeypatch):
    assert credentials.get("upstox_api_secret") == "" and credentials.source("upstox_api_secret") is None
    monkeypatch.setattr(settings, "upstox_api_secret", SecretStr(SECRET))
    assert credentials.get("upstox_api_secret") == SECRET and credentials.source("upstox_api_secret") == ".env"
    credentials.set("upstox_api_secret", SECRET + "x")
    assert credentials.get("upstox_api_secret") == SECRET + "x"
    assert credentials.source("upstox_api_secret") == "keychain"
    assert credentials.conflicts() == ["upstox_api_secret"]


def test_a_conflict_is_warned_about_once(monkeypatch, caplog):
    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr("123456789:AAH-envenvenvenvenvenvenv"))
    credentials.set("telegram_bot_token", "123456789:AAH-keykeykeykeykeykeykey")
    with caplog.at_level(logging.WARNING):
        credentials.get("telegram_bot_token")
        credentials.get("telegram_bot_token")
    assert sum("different values" in r.message for r in caplog.records) == 1


def test_bad_values_and_names_are_refused():
    with pytest.raises(ValueError):
        credentials.set("upstox_api_secret", "   ")
    with pytest.raises(ValueError):
        credentials.set("upstox_api_secret", "has a space")
    with pytest.raises(KeyError):
        credentials.get("nope")


def test_no_keychain_explains_what_to_do(monkeypatch):
    from keyring.errors import NoKeyringError

    def broken(*_a, **_k):
        raise NoKeyringError("no backend")

    monkeypatch.setattr(keyring, "set_password", broken)
    monkeypatch.setattr(keyring, "get_password", broken)
    with pytest.raises(credentials.SecretStoreUnavailable, match="UPSTOX_API_SECRET"):
        credentials.set("upstox_api_secret", SECRET)
    monkeypatch.setattr(settings, "upstox_api_secret", SecretStr(SECRET))
    assert credentials.get("upstox_api_secret") == SECRET, "reads still fall back to .env"


def test_a_keychain_secret_is_redacted_before_first_use():
    credentials.set("upstox_api_secret", SECRET)
    logsetup._registered.discard(SECRET)
    stream = io.StringIO()
    root, level = logging.getLogger(), logging.getLogger().level
    try:
        logsetup.configure_logging("INFO", stream=stream, to_file=False)
        logging.getLogger("backend.test").error("upstream echoed %s", SECRET)
    finally:
        logsetup.reset_logging()
        root.setLevel(level)
    assert SECRET not in stream.getvalue() and logsetup.REDACTED in stream.getvalue()


# ---------------------------------------------------------------------------
# Upstox
# ---------------------------------------------------------------------------

def test_the_login_session_goes_to_the_keychain_not_a_file():
    from backend.data import upstox_auth

    token = _jwt(datetime.now(IST) + timedelta(hours=5))
    upstox_auth._save_token({"access_token": token, "user_id": "U1"})
    assert not settings.token_path.exists()
    assert json.loads(credentials.get("upstox_session"))["access_token"] == token
    assert upstox_auth.token_status().valid and upstox_auth.load_token() == token


def test_a_legacy_cache_file_moves_into_the_keychain():
    from backend.data import upstox_auth

    token = _jwt(datetime.now(IST) + timedelta(hours=5))
    settings.token_path.write_text(json.dumps({
        "access_token": token, "expires_at": (datetime.now(IST) + timedelta(hours=5)).isoformat(),
    }))
    assert upstox_auth.load_token() == token
    assert not settings.token_path.exists(), "the plaintext file should be gone"
    assert upstox_auth.load_token() == token, "and the keychain copy should be used from now on"


def test_without_a_keychain_the_session_file_is_kept_private(monkeypatch):
    from backend.data import upstox_auth

    def unavailable(name, value):
        raise credentials.SecretStoreUnavailable("no keychain")

    monkeypatch.setattr(credentials, "set", unavailable)
    upstox_auth._save_token({"access_token": _jwt(datetime.now(IST) + timedelta(hours=5))})
    assert settings.token_path.stat().st_mode & 0o777 == 0o600


def test_a_pasted_token_wins_and_reports_where_it_lives(monkeypatch):
    from backend.data import upstox_auth

    upstox_auth._save_token({"access_token": _jwt(datetime.now(IST) + timedelta(hours=5))})
    pasted = _jwt(datetime.now(IST) + timedelta(hours=8))
    credentials.set("upstox_access_token", pasted)
    status = upstox_auth.token_status()
    assert status.valid and "keychain" in status.reason and upstox_auth.load_token() == pasted

    credentials.delete("upstox_access_token")
    monkeypatch.setattr(settings, "upstox_access_token", SecretStr(_jwt(datetime.now(IST) - timedelta(hours=1))))
    status = upstox_auth.token_status()
    assert not status.valid and ".env" in status.reason and "credentials set" in status.reason


def test_the_exchange_uses_the_keychain_secret(monkeypatch):
    from backend.data import upstox_auth

    monkeypatch.setattr(settings, "upstox_api_key", "client-id")
    credentials.set("upstox_api_secret", SECRET)
    sent = {}

    class Response:
        status_code = 200

        def json(self):
            return {"access_token": _jwt(datetime.now(IST) + timedelta(hours=5))}

    def post(url, data, headers, timeout):
        sent.update(data)
        return Response()

    monkeypatch.setattr(upstox_auth.httpx, "post", post)
    upstox_auth.exchange_code("code123")
    assert sent["client_secret"] == SECRET


def test_alerts_read_the_bot_token_from_the_keychain(monkeypatch):
    from backend.live.alerts import Alerts

    monkeypatch.setattr(settings, "telegram_chat_id", "42")
    credentials.set("telegram_bot_token", "123456789:AAH-keychainkeychainkeychain")
    alerts = Alerts()
    assert alerts.enabled and alerts.token.endswith("keychainkeychainkeychain")


# ---------------------------------------------------------------------------
# Import from .env, CLI, API
# ---------------------------------------------------------------------------

def test_import_moves_secrets_out_of_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(f"UPSTOX_API_KEY=abc\nUPSTOX_API_SECRET={SECRET}\nexport TELEGRAM_BOT_TOKEN=123456789:AAH-tok\nLOG_LEVEL=INFO\n")
    env.chmod(0o644)
    monkeypatch.setattr(settings, "upstox_api_secret", SecretStr(SECRET))
    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr("123456789:AAH-tok"))

    kept = credentials.import_from_env(env_path=env)
    assert sorted(kept["imported"]) == ["telegram_bot_token", "upstox_api_secret"]
    assert kept["removed_from_env"] == [] and SECRET in env.read_text()

    result = credentials.import_from_env(remove_from_env=True, env_path=env)
    assert sorted(result["removed_from_env"]) == ["TELEGRAM_BOT_TOKEN", "UPSTOX_API_SECRET"]
    assert env.read_text() == "UPSTOX_API_KEY=abc\nLOG_LEVEL=INFO\n"
    assert env.stat().st_mode & 0o777 == 0o600
    assert credentials.get("upstox_api_secret") == SECRET


def test_import_never_overwrites_a_different_keychain_value(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(f"UPSTOX_API_SECRET={SECRET}\n")
    monkeypatch.setattr(settings, "upstox_api_secret", SecretStr(SECRET))
    credentials.set("upstox_api_secret", SECRET + "other")
    result = credentials.import_from_env(remove_from_env=True, env_path=env)
    assert result == {"imported": [], "skipped": ["upstox_api_secret"], "removed_from_env": []}
    assert SECRET in env.read_text()


def test_cli(monkeypatch, capsys):
    monkeypatch.setattr("getpass.getpass", lambda prompt: SECRET)
    assert cli.main(["credentials", "set", "upstox_api_secret"]) == 0
    assert credentials.get("upstox_api_secret") == SECRET
    assert cli.main(["credentials", "list"]) == 0
    out = capsys.readouterr().out
    assert "upstox_api_secret" in out and "keychain" in out and SECRET not in out
    assert cli.main(["credentials", "set"]) == 2
    assert cli.main(["credentials", "set", "nope"]) == 1
    assert cli.main(["credentials", "delete", "upstox_api_secret"]) == 0
    assert credentials.source("upstox_api_secret") is None
    logsetup.reset_logging()


def test_doctor_flags_plaintext_and_conflicts(monkeypatch):
    from backend.ops import doctor

    assert doctor._credentials().status == "ok"
    monkeypatch.setattr(settings, "upstox_api_secret", SecretStr(SECRET))
    check = doctor._credentials()
    assert check.status == "warn" and "plaintext" in check.detail and "import" in check.detail
    credentials.set("upstox_api_secret", SECRET + "x")
    assert "differ" in doctor._credentials().detail


def test_api_is_write_only():
    from fastapi.testclient import TestClient

    from backend.api.main import app

    client = TestClient(app, base_url="http://127.0.0.1:8000")
    stored = client.put("/api/credentials/upstox_api_secret", json={"value": SECRET})
    assert stored.status_code == 200 and SECRET not in stored.text
    names = {c["name"]: c["source"] for c in stored.json()["credentials"]}
    assert names["upstox_api_secret"] == "keychain"
    assert SECRET not in client.get("/api/credentials").text
    assert client.put("/api/credentials/upstox_session", json={"value": "x" * 10}).status_code == 404
    assert client.put("/api/credentials/nope", json={"value": "x" * 10}).status_code == 404
    assert client.put("/api/credentials/upstox_api_secret", json={"value": "with a space"}).status_code == 400
    assert client.delete("/api/credentials/upstox_api_secret").json()["credentials"][0]["source"] is None
