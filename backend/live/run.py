"""CLI front door for paper sessions.

    python -m backend.live.run paper  --template ema_crossover --symbol NIFTY -t 5m
    python -m backend.live.run replay --template ema_crossover --symbol BANKNIFTY \\
                                      -t 5m --from 2026-01-01 --speed 60
    python -m backend.live.run sessions
    python -m backend.live.run show 20260813-NSE-INDEX-Nifty-Bank-a6b2c44a

Exists because the browser is the wrong place to hold a session that has to
survive the whole trading day. A tab that gets closed, a laptop that sleeps, a
dev server that reloads on a stray file save — none of those should be able to
end a session that is holding a position. Run it here, watch it in the UI.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal

from backend.core.config import IST, Mode, settings
from backend.live import journal as J
from backend.live.session import PaperSession, SessionConfig
from backend.strategy import templates
from backend.strategy.schema import Strategy

log = logging.getLogger("live")


def _resolve(symbol: str) -> str:
    from backend.data import instruments as inst

    key = inst.resolve(symbol)
    if not key:
        sys.exit(f"Unknown instrument {symbol!r}. Try: python -m backend.data.ingest status")
    return key


def _strategy(args) -> Strategy:
    if args.file:
        return Strategy.model_validate(json.loads(open(args.file).read()))
    if args.template not in templates.TEMPLATES:
        sys.exit(f"Unknown template {args.template!r}. Options: {', '.join(templates.TEMPLATES)}")
    return templates.load(args.template, symbol=args.symbol, timeframe=args.timeframe)


def _config(args, key: str, strategy: Strategy) -> SessionConfig:
    return SessionConfig(
        strategy=strategy,
        instrument_key=key,
        starting_capital=Decimal(str(args.capital)),
        lot_size=args.lot_size,
        slippage_bps=args.slippage,
        max_leverage=Decimal(str(args.max_leverage)),
        warmup_bars=args.warmup,
        mode=Mode.PAPER,
        alerts_enabled=not args.no_alerts,
    )


def _attach_signals(session: PaperSession) -> None:
    """Ctrl-C must close the journal cleanly, not abandon it mid-write."""

    def handler(signum, _frame):
        log.warning("Signal %s — stopping session", signum)
        session.stop("signal")

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def cmd_paper(args) -> None:
    from backend.data.upstox_auth import token_status
    from backend.live.sources import RestPollingSource

    status = token_status()
    if not status.valid:
        sys.exit(
            f"Upstox token unusable: {status.reason}\n"
            f"Re-authorize first:  python -m backend.data.upstox_auth"
        )
    print(f"Token valid for {status.seconds_remaining / 3600:.1f}h (until {status.expires_at})")

    strategy = _strategy(args)
    key = _resolve(args.symbol)
    source = RestPollingSource(key, strategy.timeframe, poll_seconds=args.poll)
    session = PaperSession.create(_config(args, key, strategy), source=source)
    _attach_signals(session)

    print(f"Session {session.journal.session_id}")
    print(f"Journal {session.journal.path}")
    session.run()
    _summary(session)


def cmd_replay(args) -> None:
    from backend.data.store import read_candles
    from backend.live.sources import ReplaySource

    strategy = _strategy(args)
    key = _resolve(args.symbol)

    stored = read_candles(key, strategy.timeframe)
    if stored.empty:
        sys.exit(
            f"No stored candles for {key} at {strategy.timeframe}. Fetch them:\n"
            f"    python -m backend.data.ingest fetch {args.symbol} -t {strategy.timeframe}"
        )

    import pandas as pd

    start = pd.Timestamp(args.start).tz_localize(IST) if args.start else None
    end = pd.Timestamp(args.end).tz_localize(IST) + timedelta(days=1) if args.end else None

    frame = stored
    if end is not None:
        frame = frame[frame["timestamp"] < end]
    # The feed is handed the whole frame plus the window boundary, so a resumed
    # session can warm up past that boundary to wherever the journal stopped.
    replay = frame[frame["timestamp"] >= start] if start is not None else frame

    if replay.empty:
        sys.exit("That replay window contains no stored bars.")

    print(f"Replaying {len(replay):,} bars at "
          f"{'max speed' if not args.speed else f'{args.speed}x'}")

    config = _config(args, key, strategy)
    config.alerts_enabled = False          # a replay must not page you
    session = PaperSession.create(
        config,
        source=ReplaySource(replay.reset_index(drop=True), key, speed=args.speed),
        history_frame=frame,
        history_before=start.to_pydatetime() if start is not None else None,
    )
    _attach_signals(session)
    session.run()
    _summary(session)


def cmd_sessions(args) -> None:
    ids = J.sessions()
    if not ids:
        print("No journaled sessions yet.")
        return
    print(f"{'SESSION':<46} {'STATE':<12} {'BARS':>7} {'TRADES':>7}  STRATEGY")
    for sid in ids[: args.limit]:
        state = J.recover(sid)
        if not state.started:
            continue
        label = "ended" if state.ended else "INTERRUPTED"
        print(f"{sid:<46} {label:<12} {state.bars_seen:>7} {len(state.trades):>7}  "
              f"{state.strategy.get('name', '?')}")


def cmd_show(args) -> None:
    state = J.recover(args.session_id)
    if not state.started:
        sys.exit(f"No journal for {args.session_id}")

    net = sum(float(t["net_pnl"]) for t in state.trades)
    fees = sum(float(t["fees"]) for t in state.trades)
    print(f"Session   {state.session_id}")
    print(f"Strategy  {state.strategy.get('name', '?')}  ({state.strategy_hash[:12]})")
    print(f"Symbol    {state.symbol}  {state.timeframe}")
    print(f"State     {'ended' if state.ended else 'INTERRUPTED — resumable'}")
    print(f"Bars      {state.bars_seen:,}   last {state.last_bar_time}")
    print(f"Trades    {len(state.trades)}   net {net:+,.2f}   fees {fees:,.2f}")
    if state.open_legs:
        print(f"OPEN      {', '.join(state.open_legs)}")
        for leg in state.open_legs:
            p = state.positions[leg]
            print(f"          {leg}: {p['quantity']:+d} @ {p['average_price']} "
                  f"stop {p.get('stop_price')} target {p.get('target_price')}")
    if state.errors:
        print(f"Errors    {len(state.errors)}")
        for e in state.errors[-5:]:
            print(f"          {e}")


def _summary(session: PaperSession) -> None:
    snap = session.snapshot()
    print("\n" + "=" * 66)
    print(f"  {snap['strategy']['name']}  ·  {snap['symbol']}  ·  {snap['state']}")
    print("=" * 66)
    print(f"  bars           {snap['bars_seen']:,}")
    print(f"  trades         {len(snap['trades'])}")
    print(f"  realized       {snap['realized_pnl']:+,.2f}")
    print(f"  fees           {snap['fees_paid']:,.2f}")
    print(f"  net P&L        {snap['net_pnl']:+,.2f}")
    print(f"  equity         {snap['equity']:,.2f}")
    print(f"  reconciled     {snap['reconciled']}")
    if snap["blocked"]:
        print(f"  blocked        {snap['blocked']}")
    if snap["errors"]:
        print(f"  errors         {snap['errors']}")
    print(f"  journal        {session.journal.path}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="backend.live.run", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--template", default="ema_crossover")
        p.add_argument("--file", help="strategy JSON document instead of a template")
        p.add_argument("--symbol", default="NIFTY")
        p.add_argument("-t", "--timeframe", default="5m")
        p.add_argument("--capital", type=float, default=1_000_000)
        p.add_argument("--lot-size", type=int, default=1)
        p.add_argument("--slippage", type=float, default=5.0)
        p.add_argument("--max-leverage", type=float, default=5.0)
        p.add_argument("--warmup", type=int, default=400)
        p.add_argument("--no-alerts", action="store_true")

    p_paper = sub.add_parser("paper", help="run against the live Upstox feed")
    common(p_paper)
    p_paper.add_argument("--poll", type=float, default=5.0, help="seconds between polls")
    p_paper.set_defaults(func=cmd_paper)

    p_replay = sub.add_parser("replay", help="run the live path over stored candles")
    common(p_replay)
    p_replay.add_argument("--from", dest="start", help="YYYY-MM-DD")
    p_replay.add_argument("--to", dest="end", help="YYYY-MM-DD")
    p_replay.add_argument("--speed", type=float, default=0.0, help="0 = as fast as possible")
    p_replay.set_defaults(func=cmd_replay)

    p_list = sub.add_parser("sessions", help="list journaled sessions")
    p_list.add_argument("--limit", type=int, default=25)
    p_list.set_defaults(func=cmd_sessions)

    p_show = sub.add_parser("show", help="inspect one session's journal")
    p_show.add_argument("session_id")
    p_show.set_defaults(func=cmd_show)

    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    args.func(args)


if __name__ == "__main__":
    main()
