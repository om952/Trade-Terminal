"""Append-only session journal — the record of what actually happened.

A backtest can be re-run to find out what it did. A live session cannot: the
market it traded against is gone. So every bar, signal, order and fill is written
to disk as it happens, in order, and never rewritten.

Why append-only rather than a state table: a crash halfway through updating a
mutable row leaves you unable to tell whether the update applied. A crash
halfway through appending a line leaves a torn last line and an intact prefix —
and a torn last line is trivially detectable. Recovery reads the prefix.

What recovery restores is *recorded* state, not *re-derived* state. Every fill
carries a full snapshot of the position it produced, including its stop and
target. Those levels were computed from indicator values on the entry bar; trying
to recompute them after a restart means reconstructing indicators that may have
warmed from a different starting point, and a stop that moves because you
restarted the process is not a stop.

Durability: bars and heartbeats are flushed. Anything involving money is
flushed *and* fsynced before the call returns, because the failure mode being
guarded against is exactly the one where the process dies immediately after.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

from backend.core.config import IST, settings

log = logging.getLogger(__name__)


class Event(str, Enum):
    SESSION_START = "session_start"
    BAR = "bar"
    SIGNAL = "signal"
    ORDER = "order"
    FILL = "fill"
    TRADE = "trade"
    HEARTBEAT = "heartbeat"
    FEED_STATUS = "feed_status"
    ERROR = "error"
    SESSION_END = "session_end"


# Events after which a crash must not lose the record. Everything else is
# reconstructible from the next one that arrives.
DURABLE = {Event.SESSION_START, Event.ORDER, Event.FILL, Event.TRADE, Event.SESSION_END}


@dataclass(frozen=True, slots=True)
class Record:
    seq: int
    ts: datetime
    type: Event
    data: dict[str, Any]


def _encode(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)          # str, not float — this is money
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    raise TypeError(f"Cannot journal {type(obj).__name__}")


class Journal:
    """Writes one session's event log. Not thread-safe by design — one session,
    one writer, one file."""

    def __init__(self, session_id: str, directory: Path | None = None) -> None:
        self.session_id = session_id
        directory = directory or settings.journal_dir
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{session_id}.jsonl"
        self._seq = self._resume_seq()
        self._fh = self.path.open("a", encoding="utf-8")

    def _resume_seq(self) -> int:
        """Continue numbering if this session is being resumed after a crash."""
        if not self.path.exists():
            return 0
        last = 0
        for record in read(self.path):
            last = record.seq
        return last

    def append(self, event: Event, data: dict[str, Any]) -> Record:
        self._seq += 1
        record = Record(seq=self._seq, ts=datetime.now(IST), type=event, data=data)
        line = json.dumps(
            {
                "seq": record.seq,
                "ts": record.ts.isoformat(),
                "type": event.value,
                "data": data,
            },
            default=_encode,
            separators=(",", ":"),
        )
        self._fh.write(line + "\n")
        self._fh.flush()
        if event in DURABLE:
            os.fsync(self._fh.fileno())
        return record

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()

    def __enter__(self) -> Journal:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read(path: Path) -> Iterator[Record]:
    """Replay a journal, stopping cleanly at a torn final line.

    A truncated last line is the signature of a crash mid-write. It is expected,
    not corruption, and everything before it is valid — so this yields the intact
    prefix and warns, rather than refusing to read the file.
    """
    if not path.exists():
        return

    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                log.warning(
                    "%s: line %d is truncated — journal ends there "
                    "(process died mid-write; earlier records are intact)",
                    path.name, lineno,
                )
                return
            yield Record(
                seq=raw["seq"],
                ts=datetime.fromisoformat(raw["ts"]),
                type=Event(raw["type"]),
                data=raw["data"],
            )


def sessions(directory: Path | None = None) -> list[str]:
    """Session ids on disk, newest first."""
    directory = directory or settings.journal_dir
    if not directory.exists():
        return []
    files = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.stem for p in files]


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

# A session ended for one of these reasons was cut off by its process going
# away, not finished on purpose, so starting it again resumes it. A manual stop
# or a failed reconciliation is a real end.
RESUMABLE_END_REASONS = frozenset({"shutdown", "signal"})


@dataclass
class Recovered:
    """Everything needed to resume a session exactly where it stopped."""

    session_id: str
    started: bool = False
    ended: bool = False
    end_reason: str = ""
    source_kind: str = ""
    strategy: dict[str, Any] = field(default_factory=dict)
    strategy_hash: str = ""
    symbol: str = ""
    timeframe: str = ""
    starting_capital: Decimal = Decimal("0")
    lot_size: int = 1

    bars_seen: int = 0
    last_bar_time: datetime | None = None
    last_bar_index: int = -1

    positions: dict[str, dict[str, Any]] = field(default_factory=dict)
    portfolio: dict[str, Any] = field(default_factory=dict)
    risk: dict[str, Any] = field(default_factory=dict)
    trades: list[dict[str, Any]] = field(default_factory=list)
    filled_tags: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)

    # Intents decided on the last bar processed, which had not yet reached their
    # fill bar when the process stopped. Without these, a crash between "the
    # strategy decided to enter" and "the next bar opened" silently discards the
    # decision, and the resumed session behaves differently from one that never
    # crashed — for no reason the trade log would ever reveal.
    pending: list[dict[str, Any]] = field(default_factory=list)

    @property
    def open_legs(self) -> list[str]:
        return [leg for leg, p in self.positions.items() if int(p.get("quantity", 0)) != 0]

    @property
    def resumable(self) -> bool:
        """Started, and either never ended (a crash) or cut off by a shutdown."""
        return self.started and (not self.ended or self.end_reason in RESUMABLE_END_REASONS)


def recover(session_id: str, directory: Path | None = None) -> Recovered:
    """Rebuild session state from its journal.

    Position state is taken from the last fill that touched each leg, not folded
    forward from the first — the snapshot is authoritative, so a torn record
    somewhere in the middle cannot silently skew the running total.
    """
    directory = directory or settings.journal_dir
    path = (directory / f"{session_id}.jsonl")
    state = Recovered(session_id=session_id)

    for record in read(path):
        data = record.data
        if record.type is Event.SESSION_START:
            state.started = True
            # A start after an end is a resume; the earlier end no longer applies.
            state.ended = False
            state.end_reason = ""
            state.source_kind = data.get("source_kind", "")
            state.strategy = data.get("strategy", {})
            state.strategy_hash = data.get("strategy_hash", "")
            state.symbol = data.get("symbol", "")
            state.timeframe = data.get("timeframe", "")
            state.starting_capital = Decimal(str(data.get("starting_capital", "0")))
            state.lot_size = int(data.get("lot_size", 1))

        elif record.type is Event.BAR:
            state.bars_seen += 1
            state.last_bar_index = int(data.get("index", state.last_bar_index))
            state.last_bar_time = datetime.fromisoformat(data["t"])
            state.pending = data.get("pending", [])

        elif record.type is Event.FILL:
            state.filled_tags.add(data["client_tag"])
            state.positions[data["leg_id"]] = data["position"]
            state.portfolio = data["portfolio"]
            state.risk = data.get("risk", state.risk)

        elif record.type is Event.TRADE:
            state.trades.append(data)

        elif record.type is Event.ERROR:
            state.errors.append(data.get("message", ""))

        elif record.type is Event.SESSION_END:
            state.ended = True
            state.end_reason = data.get("reason", "")

    return state
