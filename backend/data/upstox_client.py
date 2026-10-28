"""Upstox REST client: rate-limited, retrying, and chunk-aware.

Two things this module exists to hide from callers:

1. **Rate limits.** Upstox allows 50 req/s, 500 req/min and 2000 req/30min on
   data endpoints. A naive loop over a year of 1-minute candles blows through
   all three. The limiter below tracks all three windows simultaneously.

2. **Per-request range caps.** The historical endpoint will not return a year of
   1-minute candles in one call — the window shrinks as the interval shrinks.
   `fetch_candles` splits a requested range into legal chunks, fetches each, and
   stitches them back together, so callers just ask for what they want.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

import httpx
import pandas as pd

from backend.core.config import IST
from backend.data.upstox_auth import auth_headers

log = logging.getLogger(__name__)

BASE = "https://api.upstox.com"

# Upstox data-endpoint limits, with a little headroom so a burst at the
# boundary does not trip a 429 that costs more time than the wait would have.
LIMITS = ((45, 1.0), (450, 60.0), (1900, 1800.0))  # (max_calls, window_seconds)

# Largest span we will request in a single call, per unit.
#
# Upstox rejects minute-data windows of ~29 days or more with a bare
# "Invalid date range" (UDAPI1148). Measured empirically: 28 days succeeds,
# 30 fails. This was originally set to exactly 30, which sat on the boundary —
# some ranges happened to fit and others did not, so the same code fetched NIFTY
# successfully and then failed on BANKNIFTY, which looks like an instrument
# problem and is not.
#
# 25 leaves real margin. An extra HTTP call is cheap; a fetch that dies halfway
# through a year of history is not.
CHUNK_DAYS = {"minutes": 25, "hours": 90, "days": 1825, "weeks": 3650, "months": 3650}

# Earliest data the v3 API serves, by unit.
EARLIEST = {
    "minutes": date(2022, 1, 1),
    "hours": date(2022, 1, 1),
    "days": date(2000, 1, 1),
    "weeks": date(2000, 1, 1),
    "months": date(2000, 1, 1),
}


class RateLimiter:
    """Sliding-window limiter enforcing several windows at once."""

    def __init__(self, limits=LIMITS) -> None:
        self._limits = limits
        self._calls: deque[float] = deque()

    def acquire(self) -> None:
        while True:
            now = time.monotonic()
            longest = max(w for _, w in self._limits)
            while self._calls and now - self._calls[0] > longest:
                self._calls.popleft()

            wait = 0.0
            for max_calls, window in self._limits:
                in_window = sum(1 for t in self._calls if now - t <= window)
                if in_window >= max_calls:
                    oldest = next(t for t in self._calls if now - t <= window)
                    wait = max(wait, window - (now - oldest) + 0.01)

            if wait <= 0:
                self._calls.append(now)
                return
            log.debug("Rate limit: sleeping %.2fs", wait)
            time.sleep(wait)


_limiter = RateLimiter()


@dataclass(frozen=True, slots=True)
class Timeframe:
    """A timeframe expressed the way the v3 API wants it."""

    unit: str      # minutes | hours | days | weeks | months
    interval: int

    @property
    def label(self) -> str:
        """Short form used for storage paths: 1m, 5m, 1h, 1d."""
        suffix = {"minutes": "m", "hours": "h", "days": "d", "weeks": "w", "months": "M"}
        return f"{self.interval}{suffix[self.unit]}"

    @classmethod
    def parse(cls, text: str) -> Timeframe:
        """'5m' -> minutes/5, '1d' -> days/1."""
        text = text.strip().lower()
        units = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
        if text.endswith("mo"):
            return cls("months", int(text[:-2] or 1))
        suffix = text[-1]
        if suffix not in units:
            raise ValueError(f"Unrecognized timeframe {text!r}. Try 1m, 5m, 15m, 1h, 1d.")
        return cls(units[suffix], int(text[:-1] or 1))


def _request(path: str, *, timeout: float = 60.0, retries: int = 4) -> dict:
    """GET with rate limiting and retry on transient failures."""
    url = f"{BASE}{path}"
    last_exc: Exception | None = None

    for attempt in range(retries):
        _limiter.acquire()
        try:
            resp = httpx.get(url, headers=auth_headers(), timeout=timeout)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            backoff = 2**attempt
            log.warning("Network error (%s), retry %d in %ds", type(exc).__name__, attempt + 1, backoff)
            time.sleep(backoff)
            continue

        if resp.status_code == 200:
            return resp.json()

        if resp.status_code == 429:
            backoff = 2 ** (attempt + 1)
            log.warning("Rate limited by Upstox, backing off %ds", backoff)
            time.sleep(backoff)
            continue

        if resp.status_code == 401:
            raise RuntimeError(
                "Upstox rejected the token (401). It expires daily at 09:00 IST — "
                "re-run: python -m backend.data.upstox_auth"
            )

        if 500 <= resp.status_code < 600:
            backoff = 2**attempt
            log.warning("Upstox %s, retry %d in %ds", resp.status_code, attempt + 1, backoff)
            time.sleep(backoff)
            continue

        raise RuntimeError(f"Upstox {resp.status_code} for {path}: {resp.text[:300]}")

    raise RuntimeError(f"Upstox request failed after {retries} attempts: {path}") from last_exc


def _candles_to_frame(candles: list[list]) -> pd.DataFrame:
    """v3 returns [ts, open, high, low, close, volume, oi], newest first."""
    if not candles:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])

    df = pd.DataFrame(
        candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"]
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, format="mixed").dt.tz_convert(IST)
    return df.sort_values("timestamp").reset_index(drop=True)


def fetch_chunk(instrument_key: str, tf: Timeframe, from_date: date, to_date: date) -> pd.DataFrame:
    """One API call. Callers should prefer `fetch_candles`, which chunks for you."""
    path = (
        f"/v3/historical-candle/{instrument_key}/{tf.unit}/{tf.interval}"
        f"/{to_date:%Y-%m-%d}/{from_date:%Y-%m-%d}"
    )
    payload = _request(path)
    return _candles_to_frame(payload.get("data", {}).get("candles", []))


def fetch_intraday(instrument_key: str, tf: Timeframe) -> pd.DataFrame:
    """Today's bars. The historical endpoint excludes the current session."""
    path = f"/v3/historical-candle/intraday/{instrument_key}/{tf.unit}/{tf.interval}"
    payload = _request(path)
    return _candles_to_frame(payload.get("data", {}).get("candles", []))


def fetch_candles(
    instrument_key: str,
    timeframe: str | Timeframe,
    from_date: date,
    to_date: date | None = None,
    include_today: bool = True,
    on_progress: Callable[[int, int], None] | None = None,
) -> pd.DataFrame:
    """Fetch a full range, chunking as needed. This is the function to call.

    Clamps `from_date` to the earliest date Upstox actually serves for the unit,
    so asking for 1-minute data from 2015 returns 2022-onward rather than an error.

    `on_progress(done, total)` is called before each chunk request. It may raise
    to abandon the fetch (how a background job is cancelled).
    """
    tf = Timeframe.parse(timeframe) if isinstance(timeframe, str) else timeframe
    today = pd.Timestamp.now(tz=IST).date()
    to_date = to_date or today

    earliest = EARLIEST[tf.unit]
    if from_date < earliest:
        log.info("Clamping from_date %s to %s (earliest Upstox serves for %s)",
                 from_date, earliest, tf.unit)
        from_date = earliest

    if from_date > to_date:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])

    span = CHUNK_DAYS[tf.unit]
    frames: list[pd.DataFrame] = []
    cursor = from_date
    chunks = 0
    failed: list[tuple[date, date, str]] = []
    total_chunks = (to_date - from_date).days // span + 1

    while cursor <= to_date:
        if on_progress is not None:
            on_progress(chunks, total_chunks)
        chunk_end = min(cursor + timedelta(days=span - 1), to_date)
        try:
            df = fetch_chunk(instrument_key, tf, cursor, chunk_end)
        except RuntimeError as exc:
            # One bad window must not destroy an otherwise good fetch. Upstox
            # rejects some historical ranges (delisted contracts, pre-listing
            # dates, occasional server-side gaps); keep what we can get and
            # report the holes rather than returning nothing.
            failed.append((cursor, chunk_end, str(exc)[:120]))
            log.warning("Chunk %s..%s failed, continuing: %s", cursor, chunk_end, str(exc)[:160])
            cursor = chunk_end + timedelta(days=1)
            chunks += 1
            continue

        chunks += 1
        if not df.empty:
            frames.append(df)
        log.debug("  %s %s..%s -> %d bars", tf.label, cursor, chunk_end, len(df))
        cursor = chunk_end + timedelta(days=1)

    if failed:
        log.warning(
            "%s %s: %d of %d chunks failed (%s). Returning partial history.",
            instrument_key, tf.label, len(failed), chunks,
            ", ".join(f"{a}..{b}" for a, b, _ in failed[:3]),
        )

    if include_today and to_date >= today:
        try:
            intraday = fetch_intraday(instrument_key, tf)
            if not intraday.empty:
                frames.append(intraday)
        except RuntimeError as exc:
            # Outside market hours this is routinely empty or unavailable; the
            # historical range is still valid, so this must not fail the fetch.
            log.debug("Intraday fetch skipped: %s", exc)

    # Callers that remember "no data for this session" must not do so for a
    # window that merely failed, so the failed windows travel with the result.
    failed_ranges = [(a, b) for a, b, _ in failed]

    if not frames:
        log.warning("No data for %s %s (%s..%s) across %d chunks",
                    instrument_key, tf.label, from_date, to_date, chunks)
        empty = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
        empty.attrs["failed_ranges"] = failed_ranges
        return empty

    out = pd.concat(frames, ignore_index=True)
    out = out.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    log.info("%s %s: %d bars (%s..%s) in %d calls",
             instrument_key, tf.label, len(out),
             out["timestamp"].iloc[0].date(), out["timestamp"].iloc[-1].date(), chunks)
    out = out.reset_index(drop=True)
    out.attrs["failed_ranges"] = failed_ranges
    return out


def profile() -> dict:
    """Whoami — the cheapest way to confirm a token actually works."""
    return _request("/v2/user/profile").get("data", {})


def funds() -> dict:
    return _request("/v2/user/get-funds-and-margin").get("data", {})
