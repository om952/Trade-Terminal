"""Upstox V3 market-data WebSocket: connect, subscribe, decode, reconnect.

The wire format is protobuf, and the schema is Upstox's. It is vendored at
`proto/MarketDataFeedV3.proto` and compiled to `_pb2.py` rather than hand-decoded,
because guessing field numbers off documentation produces a decoder that runs
without error and returns wrong prices — which is the worst possible failure for
this system, since nothing downstream can tell a wrong price from a real one.

Regenerate after updating the .proto:

    protoc --python_out=backend/live/proto --proto_path=backend/live/proto \\
           MarketDataFeedV3.proto

Reconnection is not optional. A market-data socket will drop — mid-session,
without warning, on a perfectly good connection. What matters is that the drop
is *visible*: a silent reconnect that loses ticks produces a bar with a hole in
it and no indication anything went wrong, so every disconnect is journaled and
surfaced, and the session decides whether to keep trading through it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue as _queue
import ssl
import threading
import uuid
from datetime import datetime

import httpx

from backend.core.config import IST
from backend.data.upstox_auth import auth_headers
from backend.live.aggregator import Tick

log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://api.upstox.com/v3/feed/market-data-feed/authorize"
DIRECT_URL = "wss://api.upstox.com/v3/feed/market-data-feed"

MAX_BACKOFF = 30.0


class FeedDisconnected(Exception):
    """Raised into the tick queue so the consumer sees the gap rather than a pause."""


def _pb():
    try:
        from backend.live.proto import MarketDataFeedV3_pb2 as pb
    except ImportError as exc:  # pragma: no cover - setup problem, not runtime
        raise RuntimeError(
            "Protobuf decoder missing. Generate it with:\n"
            "    protoc --python_out=backend/live/proto "
            "--proto_path=backend/live/proto MarketDataFeedV3.proto"
        ) from exc
    return pb


def authorize() -> str:
    """Exchange the access token for a signed socket URL."""
    resp = httpx.get(AUTHORIZE_URL, headers=auth_headers(), timeout=30.0)
    if resp.status_code != 200:
        raise RuntimeError(f"Feed authorize failed ({resp.status_code}): {resp.text[:200]}")
    uri = resp.json().get("data", {}).get("authorized_redirect_uri")
    if not uri:
        raise RuntimeError(f"No authorized_redirect_uri in response: {resp.text[:200]}")
    return uri


def decode(payload: bytes) -> list[Tick]:
    """Turn one protobuf frame into ticks.

    Indices and tradeable instruments arrive in different branches of the oneof:
    an index has no traded volume (it is calculated, not traded), so `cum_volume`
    stays None for them and the aggregator falls back accordingly. Reporting zero
    instead would look like a real reading of no activity.
    """
    pb = _pb()
    response = pb.FeedResponse()
    response.ParseFromString(payload)

    fallback_ts = _epoch_ms(response.currentTs) or datetime.now(IST)
    ticks: list[Tick] = []

    for key, feed in response.feeds.items():
        which = feed.WhichOneof("FeedUnion")
        ltpc = cum_volume = oi = None

        if which == "ltpc":
            ltpc = feed.ltpc
        elif which == "fullFeed":
            branch = feed.fullFeed.WhichOneof("FullFeedUnion")
            if branch == "marketFF":
                ltpc = feed.fullFeed.marketFF.ltpc
                cum_volume = feed.fullFeed.marketFF.vtt
                oi = int(feed.fullFeed.marketFF.oi) or None
            elif branch == "indexFF":
                ltpc = feed.fullFeed.indexFF.ltpc
        elif which == "firstLevelWithGreeks":
            ltpc = feed.firstLevelWithGreeks.ltpc
            cum_volume = feed.firstLevelWithGreeks.vtt
            oi = int(feed.firstLevelWithGreeks.oi) or None

        if ltpc is None or not ltpc.ltp:
            continue

        ticks.append(
            Tick(
                symbol=key,
                timestamp=_epoch_ms(ltpc.ltt) or fallback_ts,
                ltp=float(ltpc.ltp),
                ltq=int(ltpc.ltq or 0),
                cum_volume=int(cum_volume) if cum_volume else None,
                oi=oi,
            )
        )
    return ticks


def _epoch_ms(value: int | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromtimestamp(int(value) / 1000.0, tz=IST)


class UpstoxTickStream:
    """Runs the socket on a background thread, pushing ticks into a queue.

    The consumer never touches asyncio. It gets `Tick` objects, and on a drop it
    gets a `FeedDisconnected` instance through the same queue — an in-band signal,
    so a gap can never be mistaken for a quiet market.
    """

    def __init__(self, instrument_keys: list[str], mode: str = "full") -> None:
        self.instrument_keys = instrument_keys
        self.mode = mode
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._inbox: _queue.Queue | None = None
        self.connected = False
        self.reconnects = 0

    def start(self, inbox: _queue.Queue) -> None:
        self._inbox = inbox
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="upstox-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    # -- background thread ---------------------------------------------------

    def _run(self) -> None:
        try:
            asyncio.run(self._loop())
        except Exception as exc:  # pragma: no cover - thread-level guard
            log.exception("Feed thread died")
            self._push(FeedDisconnected(f"feed thread died: {exc}"))

    async def _loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._session()
                backoff = 1.0                      # a clean run resets the ladder
            except Exception as exc:
                if self._stop.is_set():
                    return
                self.connected = False
                self.reconnects += 1
                self._push(FeedDisconnected(f"{type(exc).__name__}: {exc}"))
                log.warning("Feed disconnected (%s); reconnecting in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)

    async def _session(self) -> None:
        from websockets.asyncio.client import connect

        uri = authorize()
        context = ssl.create_default_context()

        async with connect(uri, ssl=context, max_size=None, ping_interval=20) as ws:
            self.connected = True
            log.info("Feed connected: %d instrument(s), mode=%s",
                     len(self.instrument_keys), self.mode)
            await ws.send(json.dumps({
                "guid": str(uuid.uuid4()),
                "method": "sub",
                "data": {"mode": self.mode, "instrumentKeys": self.instrument_keys},
            }).encode("utf-8"))

            while not self._stop.is_set():
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue                        # idle socket, not a failure
                if isinstance(message, str):
                    message = message.encode("utf-8")
                for tick in decode(message):
                    self._push(tick)

    def _push(self, item: object) -> None:
        if self._inbox is not None:
            self._inbox.put(item)
