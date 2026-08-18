"""Upstox OAuth 2.0 — authorization code flow, token cache, expiry handling.

Upstox tokens are NOT refreshable. There is no refresh_token grant: every token
dies at 03:30 IST daily and you must re-authorize through the browser. That is a
broker constraint, not a design choice, and it is the single most common way an
unattended live system silently stops working.

Note the timing: 03:30 IST is BEFORE the 09:15 open, so a token obtained in the
evening is already dead by the next session. Re-authorization is a every-morning
ritual, not an occasional one.

So this module does two things beyond the happy path:
  1. Reads the real `exp` claim out of the JWT rather than trusting a fixed rule.
  2. Exposes `token_status()` so a pre-market health check can alert you BEFORE
     the open rather than at the first rejected order.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import secrets
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx

from backend.core.config import IST, settings

log = logging.getLogger(__name__)

AUTH_URL = "https://api.upstox.com/v2/login/authorization/dialog"
TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"

# Upstox invalidates all access tokens daily at 03:30 IST. Used only as a
# fallback when a token's JWT `exp` claim cannot be parsed.
TOKEN_EXPIRY_HOUR_IST = 3
TOKEN_EXPIRY_MINUTE_IST = 30


@dataclass(frozen=True, slots=True)
class TokenStatus:
    valid: bool
    expires_at: datetime | None
    reason: str

    @property
    def seconds_remaining(self) -> float:
        if not self.expires_at:
            return 0.0
        return max(0.0, (self.expires_at - datetime.now(IST)).total_seconds())


def _next_expiry(now: datetime | None = None) -> datetime:
    """The next 09:00 IST boundary strictly after `now`."""
    now = now or datetime.now(IST)
    today_expiry = now.replace(
        hour=TOKEN_EXPIRY_HOUR_IST,
        minute=TOKEN_EXPIRY_MINUTE_IST,
        second=0,
        microsecond=0,
    )
    return today_expiry if now < today_expiry else today_expiry + timedelta(days=1)


def build_authorize_url(state: str | None = None) -> tuple[str, str]:
    """Return (url, state). Open the url in a browser to start the flow.

    `state` is echoed back on the redirect; we verify it to reject a callback
    that did not originate from this process.
    """
    settings.require_upstox()
    state = state or secrets.token_urlsafe(16)
    params = {
        "client_id": settings.upstox_api_key,
        "redirect_uri": settings.upstox_redirect_uri,
        "response_type": "code",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}", state


def exchange_code(code: str) -> dict:
    """Swap a single-use authorization code for an access token, and cache it.

    The code is burned on first use whether or not the exchange succeeds, so a
    retry needs a fresh trip through the browser.
    """
    settings.require_upstox()
    payload = {
        "code": code,
        "client_id": settings.upstox_api_key,
        "client_secret": settings.upstox_api_secret.get_secret_value(),
        "redirect_uri": settings.upstox_redirect_uri,
        "grant_type": "authorization_code",
    }
    resp = httpx.post(
        TOKEN_URL,
        data=payload,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        timeout=30.0,
    )
    if resp.status_code != 200:
        # Never echo the body verbatim at error level — it can contain the code.
        log.error("Token exchange failed: HTTP %s", resp.status_code)
        raise RuntimeError(f"Upstox token exchange failed ({resp.status_code}): {resp.text[:300]}")

    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"No access_token in Upstox response: {list(data.keys())}")

    _save_token(data)
    return data


def _save_token(data: dict) -> None:
    record = {
        "access_token": data["access_token"],
        "user_id": data.get("user_id"),
        "user_name": data.get("user_name"),
        "email": data.get("email"),
        "broker": data.get("broker"),
        "issued_at": datetime.now(IST).isoformat(),
        "expires_at": _next_expiry().isoformat(),
    }
    settings.token_path.parent.mkdir(parents=True, exist_ok=True)
    settings.token_path.write_text(json.dumps(record, indent=2))
    settings.token_path.chmod(0o600)  # bearer credential; keep it owner-only
    log.info("Token cached, expires %s", record["expires_at"])


def _jwt_expiry(token: str) -> datetime | None:
    """Read the `exp` claim from an Upstox JWT without verifying the signature.

    We are not authenticating the token — the broker does that. We only want to
    know when it dies, so a pre-market health check can warn before the open
    rather than discovering it at the first rejected order. A token we cannot
    parse is treated as unknown-expiry, not invalid.
    """
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)  # restore stripped padding
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = claims.get("exp")
        return datetime.fromtimestamp(int(exp), tz=IST) if exp else None
    except (IndexError, ValueError, TypeError, json.JSONDecodeError, binascii.Error):
        return None


def load_token() -> str | None:
    """Return a currently-valid access token, or None."""
    status = token_status()
    if not status.valid:
        return None

    env_token = settings.upstox_access_token.get_secret_value().strip()
    if env_token:
        return env_token
    return json.loads(settings.token_path.read_text())["access_token"]


def token_status() -> TokenStatus:
    """Inspect the active token without raising. Use this for health checks.

    A token pasted into .env wins over the cached file — it is the more explicit
    action, so silently preferring a stale cached token would be surprising.
    """
    env_token = settings.upstox_access_token.get_secret_value().strip()
    if env_token:
        expires_at = _jwt_expiry(env_token)
        if expires_at is None:
            # Unparseable JWT: fall back to the daily 09:00 IST rule rather than
            # refusing to try. The API is the real authority on validity.
            return TokenStatus(True, _next_expiry(), "using UPSTOX_ACCESS_TOKEN from .env (expiry unknown)")
        if datetime.now(IST) >= expires_at:
            return TokenStatus(
                False, expires_at,
                "UPSTOX_ACCESS_TOKEN in .env has expired — paste a fresh one or "
                "run: python -m backend.data.upstox_auth",
            )
        return TokenStatus(True, expires_at, "using UPSTOX_ACCESS_TOKEN from .env")

    if not settings.token_path.exists():
        return TokenStatus(False, None, "no cached token — run: python -m backend.data.upstox_auth")

    try:
        record = json.loads(settings.token_path.read_text())
        expires_at = datetime.fromisoformat(record["expires_at"])
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        return TokenStatus(False, None, f"token cache unreadable: {exc}")

    if datetime.now(IST) >= expires_at:
        return TokenStatus(False, expires_at, "token expired — re-authorize")

    return TokenStatus(True, expires_at, "ok")


def auth_headers() -> dict[str, str]:
    """Headers for authenticated calls. Raises if there is no valid token."""
    token = load_token()
    if not token:
        raise RuntimeError(f"No valid Upstox token: {token_status().reason}")
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def interactive_login() -> dict:
    """Run the full browser flow from the terminal.

    Upstox requires an https redirect URI, and standing up a trusted local TLS
    listener just to catch one query parameter is more moving parts than this
    deserves. The browser will land on an unreachable https://127.0.0.1 page —
    that is expected. The authorization code is sitting in that URL, so we ask
    for a paste rather than pretending to serve it.
    """
    url, state = build_authorize_url()

    print("\n" + "=" * 72)
    print("UPSTOX LOGIN")
    print("=" * 72)
    print("\n1. A browser window will open. Log in and approve access.")
    print("2. You will land on an https://127.0.0.1:8000/... page that fails to load.")
    print("   That is expected — nothing is listening there.")
    print("3. Copy the FULL URL from the address bar and paste it below.\n")
    print(f"If the browser does not open, visit:\n{url}\n")

    try:
        webbrowser.open(url)
    except Exception:  # headless / no browser configured
        pass

    time.sleep(1)
    raw = input("Paste the full redirect URL (or just the code): ").strip()

    code = _extract_code(raw, expected_state=state)
    data = exchange_code(code)
    print(f"\nAuthenticated as {data.get('user_name') or data.get('user_id')}")
    print(f"Token valid until {_next_expiry():%Y-%m-%d %H:%M %Z}\n")
    return data


def _extract_code(raw: str, expected_state: str | None = None) -> str:
    """Pull `code` out of a pasted redirect URL, tolerating a bare code."""
    from urllib.parse import parse_qs, urlparse

    if "code=" not in raw:
        return raw  # user pasted the bare code

    params = parse_qs(urlparse(raw).query)
    codes = params.get("code")
    if not codes:
        raise ValueError("No `code` parameter found in the pasted URL")

    if expected_state:
        returned = params.get("state", [None])[0]
        if returned and returned != expected_state:
            raise ValueError("state mismatch — this callback did not come from this session")

    return codes[0]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    interactive_login()
