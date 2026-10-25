"""Local-only request guard.

The API binds to 127.0.0.1, but that alone does not stop a web page the user
happens to have open from talking to it:

- **DNS rebinding.** A malicious site resolves its own hostname to 127.0.0.1 and
  then calls this API as if same-origin. The browser still sends the attacker's
  hostname in `Host`, so rejecting any non-local `Host` defeats it.
- **Drive-by requests.** A page on another origin can fire a "simple" POST (for
  example a form post to `/api/live/start`) without a CORS preflight. The browser
  sends its `Origin`, so rejecting any non-local `Origin` defeats it.

Requests without an `Origin` (curl, the CLI, server-to-server) are allowed:
browsers always send one on cross-origin requests, which are the threat here.

Remote access is a Phase 7 decision that comes with authentication and TLS. Until
then this guard has no "allow other hosts" switch on purpose.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

LOCAL_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})


def _hostname(value: str) -> str | None:
    """'localhost:8000' -> 'localhost'; '[::1]:8000' -> '::1'. None if malformed."""
    try:
        return urlsplit(f"//{value}").hostname
    except ValueError:
        return None


def is_local_host(host_header: str | None) -> bool:
    if not host_header:
        return False
    return _hostname(host_header.strip()) in LOCAL_HOSTNAMES


def is_local_origin(origin: str | None) -> bool:
    if origin is None:
        return True
    try:
        parts = urlsplit(origin.strip())
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and parts.hostname in LOCAL_HOSTNAMES


class LocalOnlyMiddleware:
    """Pure ASGI middleware: rejects non-local Host or Origin with 403."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        problem = None
        if not is_local_host(headers.get("host")):
            problem = "Host"
        elif not is_local_origin(headers.get("origin")):
            problem = "Origin"

        if problem is None:
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return

        detail = f"Rejected: {problem} is not local. Trade Terminal only serves this machine."
        body = json.dumps({"detail": detail}).encode()
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})
