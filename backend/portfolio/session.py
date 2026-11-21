"""Paper portfolio sessions: a portfolio run forward, one session close at a time (P4.1).

A session is a portfolio spec, a starting capital and a start date. Once a
day (PORTFOLIO_STEP_TIMES, after the history endpoint has served the previous
session's daily bar) a step:

1. loads the members' daily bars, from a warm-up period before the start up to
   the latest completed session;
2. runs the portfolio engine over them with `trade_from` set to the start,
   through the same simulated broker a backtest uses, so a paper portfolio and
   its backtest cannot drift apart;
3. checks every session day already recorded against the replay, and records
   the new ones. A recorded day is the paper record and is never rewritten:
   if the replay disagrees with it (the stored price history changed), the
   session halts and says which day, instead of quietly changing its past;
4. reports fills at the open and the orders queued for the next open, in the
   app and on Telegram when configured.

Replaying from the start each day keeps a step stateless (a crash or a missed
week costs nothing; the next step catches up) and makes the replay check
possible. The members are frozen when the session is created: the F&O list
changes over time, and ranking a different list would rewrite past days.

Paper only. Live portfolio orders wait for Phase 5's gates (regulatory check,
explicit sign-off), like every live order path.

A session does not touch the final holdout's purpose: every fill and every
rupee of P&L comes from prices after the session was created. Bars before the
start only warm up indicators.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pandas as pd

from backend.appstate.db import connection, to_json, transaction
from backend.core.config import IST
from backend.portfolio.spec import PortfolioSpec

log = logging.getLogger(__name__)

ACTIVE, STOPPED, HALTED = "active", "stopped", "halted"
# A session's daily bar is complete once the market has closed; before this
# time a bar dated today is not used even if the store has one.
DAY_COMPLETE = time(16, 0)
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class NotFound(KeyError):
    pass


def _lock(session_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(session_id, threading.Lock())


def _now() -> datetime:
    return datetime.now(IST)


def warmup_days(spec: PortfolioSpec) -> int:
    """Calendar days of history before the start, enough for the longest lookback in the spec."""
    longest = 0

    def walk(node: Any, under_params: bool = False) -> None:
        nonlocal longest
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, int) and not isinstance(v, bool) and (under_params or k in ("window", "period")):
                    longest = max(longest, v)
                walk(v, under_params or k == "params")
        elif isinstance(node, list):
            for v in node:
                walk(v, under_params)

    walk(spec.model_dump(mode="json"))
    sessions = max(longest, 50) + 20
    return int(sessions * 1.5) + 10           # about 250 sessions a year in 365 days


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def create(document: dict[str, Any], *, starting_capital: float = 1_000_000, slippage_bps: float = 5.0,
           mode: str = "paper", name: str | None = None, today: date | None = None) -> dict[str, Any]:
    from backend.live.kill_switch import require_released
    from backend.portfolio.service import validate
    from backend.scan.service import resolve_universe
    from backend.services import backtest

    require_released()
    spec = validate(document)
    if mode != "paper":
        raise ValueError("Live portfolio trading waits for Phase 5's gates (the regulatory check and your "
                         "sign-off); run it on paper.")
    if starting_capital <= 0 or not 0 <= slippage_bps <= 500:
        raise ValueError("Starting capital must be positive and slippage between 0 and 500 bps.")
    universe = resolve_universe(spec.universe)
    members = [u["key"] for u in universe if u["key"]]
    if not members:
        raise ValueError("No instrument in the universe could be resolved.")
    benchmark = backtest.resolve(spec.regime.symbol) if spec.regime else None
    start = today or _now().date()
    session_id = f"pfs_{start:%Y%m%d}_{uuid.uuid4().hex[:8]}"
    now = _now().isoformat(timespec="seconds")
    with transaction() as conn:
        conn.execute(
            """INSERT INTO portfolio_sessions (id, name, spec, spec_hash, family, members, benchmark, mode,
                   starting_capital, slippage_bps, start_date, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [session_id, name or spec.name, to_json(spec.model_dump(mode="json")), spec.content_hash(),
             spec.family(), json.dumps(members), benchmark, mode, float(starting_capital), float(slippage_bps),
             start.isoformat(), ACTIVE, now],
        )
    return get(session_id)


def stop(session_id: str, reason: str = "stopped by the trader") -> dict[str, Any]:
    _row(session_id)
    with transaction() as conn:
        conn.execute("UPDATE portfolio_sessions SET status = ?, status_reason = ?, stopped_at = ? "
                     "WHERE id = ? AND status = ?",
                     [STOPPED, reason, _now().isoformat(timespec="seconds"), session_id, ACTIVE])
    return get(session_id)


def _row(session_id: str) -> dict[str, Any]:
    row = connection().execute("SELECT * FROM portfolio_sessions WHERE id = ?", [session_id]).fetchone()
    if row is None:
        raise NotFound(session_id)
    return dict(row)


def _days(session_id: str) -> list[dict[str, Any]]:
    rows = connection().execute(
        "SELECT * FROM portfolio_session_days WHERE session_id = ? ORDER BY day", [session_id]).fetchall()
    return [{"day": r["day"], "equity": r["equity"], "cash": r["cash"], "fills": json.loads(r["fills"]),
             "rebalance": json.loads(r["rebalance"]) if r["rebalance"] else None} for r in rows]


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    state = json.loads(row["state"]) if row["state"] else {}
    return {
        "id": row["id"], "name": row["name"], "mode": row["mode"], "status": row["status"],
        "status_reason": row["status_reason"], "start_date": row["start_date"],
        "starting_capital": row["starting_capital"], "slippage_bps": row["slippage_bps"],
        "members": len(json.loads(row["members"])), "benchmark": row["benchmark"], "family": row["family"],
        "last_bar_date": row["last_bar_date"], "last_step_at": row["last_step_at"],
        "created_at": row["created_at"], "stopped_at": row["stopped_at"],
        "equity": state.get("equity"), "net_pnl": state.get("net_pnl"), "return_pct": state.get("return_pct"),
    }


def list_all() -> list[dict[str, Any]]:
    rows = connection().execute("SELECT * FROM portfolio_sessions ORDER BY created_at DESC, id DESC").fetchall()
    return [_summary(dict(r)) for r in rows]


def get(session_id: str) -> dict[str, Any]:
    row = _row(session_id)
    out = _summary(row)
    out["spec"] = json.loads(row["spec"])
    out["state"] = json.loads(row["state"]) if row["state"] else None
    out["days"] = _days(session_id)
    return out


# ---------------------------------------------------------------------------
# The daily step
# ---------------------------------------------------------------------------

def _last_complete_day(now: datetime) -> date:
    return now.date() if now.time() >= DAY_COMPLETE else now.date() - timedelta(days=1)


def _load(keys: list[str], first: date, last: date, fetch: bool, on_progress) -> tuple[dict[str, pd.DataFrame], list[dict]]:
    """Daily bars per key. Not cut at the holdout: see the module docstring."""
    from backend.data import cache

    frames: dict[str, pd.DataFrame] = {}
    missing: list[dict] = []
    for n, key in enumerate(keys):
        if on_progress is not None:
            on_progress(0.6 * n / max(1, len(keys)), f"Loading {n + 1} of {len(keys)}: {key}")
        bars = cache.get_bars(key, "1d", first, last, fetch=fetch)
        if bars.empty:
            missing.append({"symbol": key, "reason": " ".join(bars.notes) or "no daily bars"})
        else:
            frames[key] = bars.frame
    return frames, missing


def _fill_key(fill: dict[str, Any]) -> tuple:
    return (fill["symbol"], fill["side"], int(fill["quantity"]), round(float(fill["price"]), 4),
            round(float(fill["fees"]), 4))


def replay(row: dict[str, Any], *, fetch: bool, now: datetime, on_progress=None):
    """Run the engine from the warm-up through the last completed session. Returns (engine, missing)."""
    from backend.execution.costs import Segment
    from backend.live.broker import SimulatedBroker
    from backend.portfolio.engine import PortfolioEngine
    from backend.portfolio.feeds import HistoricalUniverseFeed
    from backend.strategy.runner import FillModel

    spec = PortfolioSpec.model_validate(json.loads(row["spec"]))
    members: list[str] = json.loads(row["members"])
    benchmark: str | None = row["benchmark"]
    start = date.fromisoformat(row["start_date"])
    keys = members + ([benchmark] if benchmark and benchmark not in members else [])
    frames, missing = _load(keys, start - timedelta(days=warmup_days(spec)), _last_complete_day(now), fetch,
                            on_progress)
    tradable = sorted(k for k in members if k in frames)
    if not tradable:
        raise ValueError(f"No member of the portfolio has daily bars since {start - timedelta(days=warmup_days(spec))} "
                         "(the warm-up before the start). Download them from the Screener, or connect Upstox.")
    if benchmark and benchmark not in frames:
        raise ValueError(f"The market filter's benchmark {benchmark} has no daily bars.")
    # Paper, like live, goes through the pre-trade checks (account caps, price band,
    # runaway stop). Replays are not paced; the order count runs by bar date.
    from backend.execution.pretrade import GuardedBroker, PreTradeLimits

    broker = GuardedBroker(SimulatedBroker(FillModel(slippage_bps=row["slippage_bps"], segment=Segment.EQUITY_DELIVERY)),
                           PreTradeLimits.from_settings())
    engine = PortfolioEngine(spec, broker, Decimal(str(row["starting_capital"])), tradable, benchmark,
                             strategy_id=row["id"], trade_from=start)
    engine.run(HistoricalUniverseFeed(frames))
    return engine, missing


def _replayed_days(engine) -> dict[str, dict[str, Any]]:
    days: dict[str, dict[str, Any]] = {}
    for (when, equity), (_, cash) in zip(engine.equity_curve, engine.cash_curve, strict=True):
        days[when.date().isoformat()] = {"equity": round(float(equity), 2), "cash": round(float(cash), 2),
                                         "fills": [], "rebalance": None}
    for fill in engine.fills:
        days[fill["date"]]["fills"].append({k: v for k, v in fill.items() if k != "date"})
    for r in engine.rebalances:
        if r["date"] in days:
            days[r["date"]]["rebalance"] = r
    return days


def _disagreement(recorded: list[dict[str, Any]], replayed: dict[str, dict[str, Any]]) -> str | None:
    for day in recorded:
        again = replayed.get(day["day"])
        if again is None:
            return f"{day['day']} is recorded but is no longer in the price history."
        if abs(again["equity"] - day["equity"]) > 0.01:
            return (f"On {day['day']} the recorded equity was {day['equity']:,.2f}; the stored prices now give "
                    f"{again['equity']:,.2f}.")
        if sorted(map(_fill_key, again["fills"])) != sorted(map(_fill_key, day["fills"])):
            return f"The fills recorded on {day['day']} differ from what the stored prices now give."
    return None


def _state(engine, row: dict[str, Any], missing: list[dict]) -> dict[str, Any]:
    capital = float(row["starting_capital"])
    equity = [float(v) for _, v in engine.equity_curve]
    peak, drawdown = 0.0, 0.0
    for v in equity:
        peak = max(peak, v)
        drawdown = min(drawdown, (v - peak) / peak * 100 if peak else 0.0)
    last = equity[-1] if equity else capital
    realized = sum((float(t.net_pnl) for t in engine.trades), 0.0)
    return {
        "as_of": engine.equity_curve[-1][0].date().isoformat() if engine.equity_curve else None,
        "equity": round(last, 2), "cash": round(float(engine.cash), 2),
        "net_pnl": round(last - capital, 2), "return_pct": round((last / capital - 1) * 100, 4),
        "realized_pnl": round(realized, 2), "unrealized_pnl": round(last - capital - realized, 2),
        "max_drawdown_pct": round(drawdown, 4),
        "holdings": engine.open_holdings(), "pending": engine.pending_orders(),
        "trades": [t.as_dict() for t in engine.trades], "missing": missing, "notes": list(engine.notes),
    }


def message(name: str, state: dict[str, Any], new_days: list[dict[str, Any]], *, start: str) -> str | None:
    """What a step tells the trader: fills at the open, and the orders for the next open."""
    from html import escape

    fills = [(d["day"], f) for d in new_days for f in d["fills"]]
    if not fills and not (state["pending"] and new_days):
        return None
    lines = [f"<b>{escape(name)}</b> (paper portfolio) — close of {state['as_of']}",
             f"Equity ₹{state['equity']:,.0f} ({state['return_pct']:+.2f}% since {start})"]
    if fills:
        lines.append("Filled at the open: " + "; ".join(
            f"{f['side']} {f['quantity']} {escape(f['symbol'])} @ {f['price']:,.2f}" for _, f in fills[:20]))
    if state["pending"]:
        lines.append("Orders for the next open: " + "; ".join(
            f"{o['side']} {o['quantity']} {escape(o['symbol'])}" for o in state["pending"][:20]))
    return "\n".join(lines)


def step(session_id: str, *, fetch: bool = True, now: datetime | None = None, on_progress=None,
         send: Callable[[str], bool] | None = None) -> dict[str, Any]:
    """Bring a session up to the last completed session close. Safe to repeat: nothing is recorded twice."""
    now = now or _now()
    with _lock(session_id):
        row = _row(session_id)
        if row["status"] != ACTIVE:
            return {"session_id": session_id, "status": row["status"], "skipped": True, "new_days": 0}
        engine, missing = replay(row, fetch=fetch, now=now, on_progress=on_progress)
        replayed = _replayed_days(engine)
        recorded = _days(session_id)
        stamp = now.isoformat(timespec="seconds")
        sender = send if send is not None else _telegram()

        problem = _disagreement(recorded, replayed)
        if problem is not None:
            reason = ("Halted: the price history changed under the paper record. " + problem +
                      " Recorded days are never rewritten; start a new session to continue.")
            with transaction() as conn:
                conn.execute("UPDATE portfolio_sessions SET status = ?, status_reason = ?, last_step_at = ? "
                             "WHERE id = ?", [HALTED, reason, stamp, session_id])
            sender(f"<b>{row['name']}</b> (paper portfolio): {reason}")
            log.warning("Portfolio session %s halted: %s", session_id, problem)
            return {"session_id": session_id, "status": HALTED, "reason": reason, "new_days": 0}

        last = recorded[-1]["day"] if recorded else ""
        new_days = [{"day": d, **v} for d, v in sorted(replayed.items()) if d > last]
        state = _state(engine, row, missing)
        with transaction() as conn:
            for d in new_days:
                conn.execute(
                    "INSERT INTO portfolio_session_days (session_id, day, equity, cash, fills, rebalance, recorded_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    [session_id, d["day"], d["equity"], d["cash"], to_json(d["fills"]),
                     to_json(d["rebalance"]) if d["rebalance"] else None, stamp])
            conn.execute("UPDATE portfolio_sessions SET state = ?, last_bar_date = ?, last_step_at = ? WHERE id = ?",
                         [to_json(state), state["as_of"], stamp, session_id])
        text = message(row["name"], state, new_days, start=row["start_date"])
        sent = bool(sender(text)) if text else False
        if on_progress is not None:
            on_progress(1.0, f"Recorded {len(new_days)} new session(s)")
        return {"session_id": session_id, "status": ACTIVE, "as_of": state["as_of"], "new_days": len(new_days),
                "fills": sum(len(d["fills"]) for d in new_days), "pending": state["pending"],
                "equity": state["equity"], "message": text, "sent": sent,
                "waiting": state["as_of"] is None}


def _telegram() -> Callable[[str], bool]:
    from backend.live.alerts import Alerts

    return Alerts().send


# ---------------------------------------------------------------------------
# Schedule (driven by the morning scheduler in backend/scan/alerts.py)
# ---------------------------------------------------------------------------

def owed(now: datetime, times) -> list[str]:
    from backend.scan.alerts import due

    rows = connection().execute("SELECT id, last_scheduled_at FROM portfolio_sessions WHERE status = ?",
                                [ACTIVE]).fetchall()
    return [r["id"] for r in rows if due(now, times, r["last_scheduled_at"])]


def mark_scheduled(ids: list[str], when: str) -> None:
    with transaction() as conn:
        for session_id in ids:
            conn.execute("UPDATE portfolio_sessions SET last_scheduled_at = ? WHERE id = ?", [when, session_id])
