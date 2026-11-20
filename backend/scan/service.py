"""Running a scan across a universe: resolve instruments, load bars, evaluate, aggregate.

Every instrument appears in the result, including the ones that could not be
scanned (no data, not enough history, an error) with the reason. Dropping them
silently would make a scan of 200 stocks that really covered 140 look complete.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel

from backend.appstate.db import to_json
from backend.scan import engine
from backend.scan.spec import HORIZONS, ScanSpec, Universe

Progress = Callable[[float, str], None]

NO_DATA = "no_data"
ERROR = "error"

# Order of rows in a "now" result: what just happened first.
_ORDER = {engine.TRIGGERED_TODAY: 0, engine.HOLDING: 1, engine.LAPSED: 2, engine.WAITING: 3,
          engine.NOT_ENOUGH_DATA: 4, NO_DATA: 5, ERROR: 6}

HISTORY_NOTE = (
    "Forward returns run from the next bar's open to the close h bars later, with no costs. "
    "Events on the same stock can overlap, and stocks move together, so the count overstates "
    "how many independent results this is. Compare each figure with the baseline: a signal "
    "only says something if its returns differ from simply holding over the same bars."
)
SURVIVORSHIP_NOTE = (
    "The F&O list is today's. Stocks that left it (often after falling) are missing from "
    "history, which flatters past results."
)


class ScanParams(BaseModel):
    spec: dict[str, Any]
    mode: Literal["now", "history"] = "now"
    start: date | None = None          # history mode: first bar (default: everything stored)
    end: date | None = None
    fetch: bool = True                 # download missing bars when the broker is connected


class InvalidScan(ValueError):
    def __init__(self, errors: list[dict]) -> None:
        super().__init__("; ".join(f"{e['field']}: {e['message']}" for e in errors))
        self.errors = errors


def validate(document: dict[str, Any]) -> ScanSpec:
    from backend.services.backtest import format_validation_error

    try:
        return ScanSpec.model_validate(document)
    except Exception as exc:
        raise InvalidScan(format_validation_error(exc)) from exc


def resolve_universe(universe: Universe) -> list[dict[str, str]]:
    """[{key, symbol}] for every instrument in the universe, in a stable order."""
    if universe.preset == "fno_stocks":
        from backend.data.store import read

        rows = read(
            """SELECT DISTINCT f.underlying_key, COALESCE(e.trading_symbol, f.underlying_symbol)
               FROM instruments f
               LEFT JOIN instruments e ON e.instrument_key = f.underlying_key
               WHERE f.instrument_type = 'FUT' AND f.underlying_key LIKE 'NSE_EQ|%'
               ORDER BY 2"""
        ).fetchall()
        if not rows:
            raise ValueError(
                "The F&O stock list comes from the instrument master, which has not been "
                "synced. Sync it first (python -m backend.data.instruments)."
            )
        return [{"key": key, "symbol": symbol} for key, symbol in rows]

    from backend.services.backtest import InvalidSymbol, resolve

    out = []
    for symbol in universe.symbols:
        try:
            out.append({"key": resolve(symbol), "symbol": symbol})
        except InvalidSymbol:
            out.append({"key": "", "symbol": symbol})
    return out


def _bars(key: str, start: date | None, end: date | None, fetch: bool):
    from backend.data import cache

    return cache.get_bars(key, "1d", start, end, fetch=fetch)


def run(params: ScanParams, on_progress: Progress | None = None) -> dict[str, Any]:
    spec = validate(params.spec)
    universe = resolve_universe(spec.universe)
    rows: list[dict[str, Any]] = []
    history: dict[str, dict[str, list[float]]] = {"events": {}, "baseline": {}}
    for h in HORIZONS:
        history["events"][str(h)] = []
        history["baseline"][str(h)] = []
    events: list[dict[str, Any]] = []

    for n, item in enumerate(universe):
        if on_progress is not None:
            on_progress(n / max(1, len(universe)), f"{n + 1} of {len(universe)}: {item['symbol']}")
        row: dict[str, Any] = {"symbol": item["symbol"], "key": item["key"]}
        if not item["key"]:
            rows.append({**row, "status": ERROR, "reason": f"Unknown instrument {item['symbol']!r}"})
            continue
        try:
            bars = _bars(item["key"], params.start, params.end, params.fetch)
        except Exception as exc:  # noqa: BLE001 - one bad instrument must not end the scan
            rows.append({**row, "status": ERROR, "reason": str(exc)})
            continue
        if bars.empty:
            rows.append({**row, "status": NO_DATA,
                         "reason": " ".join(bars.notes) or "No daily bars stored."})
            continue
        row["data"] = {k: v for k, v in bars.describe().items()
                       if k in ("source", "synthetic", "first_bar", "last_bar", "bars",
                                "fingerprint", "missing_sessions")}
        try:
            if params.mode == "now":
                row.update(engine.scan_now(spec, bars.frame))
            else:
                result = engine.scan_history(spec, bars.frame)
                row["status"] = result["status"]
                row["events"] = result["events"]
                for e in result["events"]:
                    events.append({"symbol": item["symbol"], **e})
                    for h, value in e["forward"].items():
                        if value is not None:
                            history["events"][h].append(value)
                for h, values in result.get("baseline", {}).items():
                    history["baseline"][h].extend(values)
                row["summary"] = {
                    h: engine.summarize([e["forward"][h] for e in result["events"]
                                         if e["forward"][h] is not None])
                    for h in map(str, HORIZONS)
                }
        except Exception as exc:  # noqa: BLE001
            rows.append({**row, "status": ERROR, "reason": f"Evaluation failed: {exc}"})
            continue
        rows.append(row)

    if on_progress is not None:
        on_progress(1.0, f"Scanned {len(universe)} instruments")
    return json.loads(to_json(_result(spec, params, rows, history, events)))


def _result(spec, params, rows, history, events) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    as_of = max((r["as_of"] for r in rows if r.get("as_of")), default=None)
    for r in rows:
        r["stale"] = bool(as_of and r.get("as_of") and r["as_of"] < as_of)
    notes = []
    if spec.universe.preset == "fno_stocks" and params.mode == "history":
        notes.append(SURVIVORSHIP_NOTE)
    if any((r.get("data") or {}).get("synthetic") for r in rows):
        notes.append("Some instruments are synthetic data, so their results say nothing about real markets.")

    out: dict[str, Any] = {
        "mode": params.mode, "spec_hash": spec.content_hash(), "name": spec.name,
        "side": spec.side, "universe": len(rows), "counts": counts, "notes": notes,
    }
    if params.mode == "now":
        rows.sort(key=lambda r: (
            _ORDER.get(r["status"], 9),
            # newest first within a status; nearest to the line first while waiting
            -(_ordinal(r.get("since") or r.get("last_fired"))),
            abs(r["distance_pct"]) if r.get("distance_pct") is not None else float("inf"),
            r["symbol"],
        ))
        out.update(as_of=as_of, rows=rows)
    else:
        out.update(
            rows=sorted(rows, key=lambda r: r["symbol"]),
            events=sorted(events, key=lambda e: (e["date"], e["symbol"])),
            summary={
                h: {"signal": engine.summarize(history["events"][h]),
                    "baseline": engine.summarize(history["baseline"][h])}
                for h in map(str, HORIZONS)
            },
        )
        notes.insert(0, HISTORY_NOTE)
    return out


def _ordinal(day: str | None) -> int:
    return date.fromisoformat(day).toordinal() if day else 0
