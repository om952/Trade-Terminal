"""Request-origin checks for the local API.

The API has no authentication because it is meant to be reachable only from the
machine it runs on. Binding to 127.0.0.1 is not enough to guarantee that, since
the user's own browser can reach loopback on behalf of any website:

* DNS rebinding: a hostile page re-points its own hostname at 127.0.0.1 and then
  talks to this API as "same origin". The request arrives with that hostname in
  its `Host` header, so refusing unknown hosts stops it.
* Drive-by requests: a page on another origin submits a form or a "simple"
  cross-origin POST to http://127.0.0.1:8000/api/live/start. CORS only stops the
  page reading the response; the request itself still runs. Browsers attach an
  `Origin` header to such requests, so refusing unknown origins stops it.

Requests with no `Origin` header (curl, the CLI, server-side clients) are
allowed: only a browser can be tricked into sending a request, and browsers
always send `Origin` on cross-origin requests that change state.

Remote deployment needs real authentication and TLS; that is P5.6. This module
is not a substitute for it.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# The Vite dev server. In production the UI is served from the API's own origin.
DEV_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")


def parse_extra_hosts(raw: str) -> frozenset[str]:
    return frozenset(h.strip().lower() for h in raw.split(",") if h.strip())


def _hostname(netloc: str) -> str | None:
    """Hostname from a `Host` header value: 'LocalHost:8000' -> 'localhost'."""
    try:
        return urlsplit(f"//{netloc.strip()}").hostname
    except ValueError:
        return None


def host_allowed(host_header: str | None, extra_hosts: frozenset[str] = frozenset()) -> bool:
    if not host_header:
        return False
    hostname = _hostname(host_header)
    return hostname is not None and (hostname in LOOPBACK_HOSTS or hostname in extra_hosts)


def origin_allowed(
    origin: str | None,
    host_header: str | None,
    allowed_origins: tuple[str, ...] = DEV_ORIGINS,
) -> bool:
    """An absent Origin passes; a present one must be the dev UI or the API itself."""
    if origin is None:
        return True
    origin = origin.strip().rstrip("/").lower()
    if origin in allowed_origins:
        return True
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    # Same origin as the API (the production case, UI served from here). This is
    # safe only because the Host header has already been checked.
    return (
        parts.scheme in ("http", "https")
        and bool(parts.netloc)
        and host_header is not None
        and parts.netloc == host_header.strip().lower()
    )


class LocalOnlyMiddleware:
    """Refuse HTTP and WebSocket requests that did not come from this machine's UI."""

    def __init__(
        self,
        app: ASGIApp,
        extra_hosts: frozenset[str] = frozenset(),
        allowed_origins: tuple[str, ...] = DEV_ORIGINS,
    ) -> None:
        self.app = app
        self.extra_hosts = extra_hosts
        self.allowed_origins = allowed_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        host = headers.get("host")
        origin = headers.get("origin")

        if not host_allowed(host, self.extra_hosts):
            reason = "Host not allowed. This API only answers on localhost."
        elif not origin_allowed(origin, host, self.allowed_origins):
            reason = "Origin not allowed. This API only accepts requests from its own UI."
        else:
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        response = JSONResponse({"detail": reason}, status_code=403)
        await response(scope, receive, send)
