"""Universe feeds: many instruments' daily bars as one stream, in time order.

Both satisfy `DataFeed`. The historical feed reads stored frames; the replay
feed runs every instrument through the paper path's `ReplaySource`, the same
component a paper session uses, and merges them. The parity test runs one
engine through each and expects identical trades and equity: if the paper
path ever builds or orders bars differently, that test is where it shows.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterator
from decimal import Decimal

import pandas as pd

from backend.core.config import IST
from backend.core.interfaces import Bar


def _key(bar: Bar) -> tuple:
    return (bar.timestamp, bar.symbol)


def _bars(frame: pd.DataFrame, symbol: str) -> Iterator[Bar]:
    for row in frame.itertuples(index=False):
        ts = pd.Timestamp(row.timestamp)
        ts = ts.tz_localize(IST) if ts.tz is None else ts.tz_convert(IST)
        yield Bar(symbol=symbol, timestamp=ts.to_pydatetime(), open=Decimal(str(row.open)),
                  high=Decimal(str(row.high)), low=Decimal(str(row.low)), close=Decimal(str(row.close)),
                  volume=int(row.volume), oi=int(getattr(row, "oi", 0) or 0) or None)


class HistoricalUniverseFeed:
    """Stored bars for every instrument, merged by (time, symbol)."""

    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self.frames = frames

    def stream(self) -> Iterator[Bar]:
        return heapq.merge(*(_bars(f, s) for s, f in sorted(self.frames.items())), key=_key)

    def warmup(self, symbol: str, bars: int) -> list[Bar]:
        return []           # the engine warms its indicators up in-stream and trades only once they are ready


class ReplayUniverseFeed:
    """Every instrument through the paper path's ReplaySource, merged by (time, symbol)."""

    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        from backend.live.sources import ReplaySource

        self.sources = [ReplaySource(f, s, speed=0.0) for s, f in sorted(frames.items())]

    def stream(self) -> Iterator[Bar]:
        return heapq.merge(*((closed.bar for closed in src.bars()) for src in self.sources), key=_key)

    def warmup(self, symbol: str, bars: int) -> list[Bar]:
        return []
