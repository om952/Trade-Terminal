"""Ingest CLI — the operational front door to the data layer.

    python -m backend.data.ingest status
    python -m backend.data.ingest instruments
    python -m backend.data.ingest fetch NIFTY --timeframe 5m --from 2024-01-01
    python -m backend.data.ingest simulate NIFTY --timeframe 5m --days 365 --seed 42
    python -m backend.data.ingest verify NIFTY --timeframe 5m
    python -m backend.data.ingest import-csv path.csv --symbol NIFTY --timeframe 1d

Fetching is resumable: it looks at what is already stored and asks only for the
missing tail. Re-running a completed fetch is close to free and changes nothing,
which matters because the recovery path for almost any data problem is "run it
again".
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta

import pandas as pd

from backend.data import instruments as inst
from backend.data.calendar import find_gaps
from backend.data.store import all_coverage, read_candles, write_candles

log = logging.getLogger("ingest")

DEFAULT_SYMBOLS = ["NIFTY", "BANKNIFTY", "SENSEX"]


def _setup_logging(verbose: bool = False) -> None:
    from backend.core.logsetup import configure_logging

    configure_logging(logging.DEBUG if verbose else logging.INFO, stream=sys.stdout)


# ---------------------------------------------------------------------------

def cmd_status(args) -> int:
    from backend.data.upstox_auth import token_status

    print("\n--- Auth ---")
    ts = token_status()
    print(f"  Upstox token : {'VALID' if ts.valid else 'INVALID'} — {ts.reason}")
    if ts.valid:
        print(f"  Expires      : {ts.expires_at:%Y-%m-%d %H:%M %Z} "
              f"({ts.seconds_remaining / 3600:.1f}h left)")

    print("\n--- Instruments ---")
    try:
        n = inst.count()
        synced = None
        from backend.data.store import read
        row = read("SELECT value FROM meta WHERE key='instruments_synced_at'").fetchone()
        if row:
            synced = row[0]
        print(f"  {n:,} contracts" + (f", synced {synced[:19]}" if synced else ""))
    except Exception as exc:
        print(f"  not synced ({exc})")

    print("\n--- Stored candles ---")
    cov = all_coverage()
    if cov.empty:
        print("  (nothing stored yet)")
    else:
        for _, r in cov.iterrows():
            print(f"  {r.instrument_key:<32} {r.timeframe:>5}  {r.bar_count:>9,} bars  "
                  f"{str(r.first_bar)[:10]} .. {str(r.last_bar)[:10]}  [{r.source}]")
    print()
    return 0


def cmd_instruments(args) -> int:
    n = inst.sync(args.exchange)
    print(f"{n:,} instruments synced from {args.exchange}")
    return 0


def _progress(fraction: float, message: str) -> None:
    print(f"  [{fraction:>4.0%}] {message}")


def cmd_fetch(args) -> int:
    """Fill the cache for a range. Intraday timeframes download 1-minute bars."""
    from backend.data.cache import get_bars

    key = inst.resolve(args.symbol)
    end = datetime.strptime(args.to_date, "%Y-%m-%d").date() if args.to_date else None
    start = (
        datetime.strptime(args.from_date, "%Y-%m-%d").date()
        if args.from_date else date.today() - timedelta(days=args.days)
    )

    print(f"Fetching {args.symbol} ({key}) {args.timeframe} from {start}...")
    bars = get_bars(key, args.timeframe, start, end, on_progress=_progress)
    for note in bars.notes:
        print(f"  note: {note}")
    if bars.empty:
        print("No data available.")
        return 1
    print(
        f"  {len(bars.frame):,} {bars.timeframe} bars "
        f"({bars.frame['timestamp'].iloc[0].date()} .. {bars.frame['timestamp'].iloc[-1].date()})"
        f"{', derived from ' + bars.derived_from if bars.derived_from else ''}; "
        f"{bars.fetched_sessions} session(s) downloaded"
    )
    return 0


def cmd_backfill(args) -> int:
    """Migrate legacy per-timeframe intraday files onto the 1-minute base.

    Downloads 1-minute bars covering the span of every stored intraday series,
    after which the cache derives those timeframes instead of reading the old
    files. The old files are left in place.
    """
    from backend.data.cache import MINUTE, broker_ready, ensure, is_fetchable

    ready, reason = broker_ready()
    if not ready:
        print(f"Cannot backfill: {reason}")
        return 1

    cov = all_coverage()
    targets = [
        r for _, r in cov.iterrows()
        if is_fetchable(r.instrument_key) and r.timeframe not in ("1m",)
        and r.timeframe[-1] in ("m", "h")
    ]
    if not targets:
        print("No legacy intraday series to migrate.")
        return 0

    failures = 0
    for r in targets:
        start = pd.Timestamp(r.first_bar).date()
        end = pd.Timestamp(r.last_bar).date()
        print(f"{r.instrument_key} {r.timeframe}: 1m base for {start} .. {end}")
        report = ensure(r.instrument_key, MINUTE, start, end, on_progress=_progress)
        print(f"  {report.fetched} downloaded, {report.empty} empty, {report.failed} failed")
        failures += report.failed
    return 1 if failures else 0


def cmd_parity(args) -> int:
    """Check that bars derived from 1-minute data equal Upstox's native bars.

    Fetches both straight from the API for a recent window and compares them
    bar by bar. Writes nothing. This is the evidence that deriving 5m, 15m and
    1h locally changes no backtest result.
    """
    from backend.data.cache import derive, session_open
    from backend.data.upstox_client import Timeframe, fetch_candles

    key = inst.resolve(args.symbol)
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=args.days)
    tf = Timeframe.parse(args.timeframe)

    minute = fetch_candles(key, "1m", start, end, include_today=False)
    native = fetch_candles(key, tf, start, end, include_today=False)
    if minute.empty or native.empty:
        print("No data returned; check the token and the symbol.")
        return 1

    derived = derive(minute, tf, session_open(key))
    merged = native.merge(derived, on="timestamp", how="outer", suffixes=("_native", "_derived"))
    only_native = merged["open_derived"].isna().sum()
    only_derived = merged["open_native"].isna().sum()
    both = merged.dropna(subset=["open_native", "open_derived"])

    tol = args.tolerance
    print(f"{key} {tf.label}, {start} .. {end}: {len(native):,} native, {len(derived):,} derived")
    print(f"  only in native: {only_native}, only in derived: {only_derived}")
    worst = 0
    for col in ("open", "high", "low", "close"):
        diff = (both[f"{col}_native"] - both[f"{col}_derived"]).abs()
        bad = int((diff > tol).sum())
        worst += bad
        print(f"  {col:<6} max |diff| {diff.max():.4f}, bars over {tol}: {bad}")
    vol = (both["volume_native"] - both["volume_derived"]).abs()
    print(f"  volume max |diff| {vol.max():,.0f} ({int((vol > 0).sum())} bars differ)")

    ok = worst == 0 and only_native == 0 and only_derived == 0
    print("PARITY OK" if ok else "PARITY FAILED")
    return 0 if ok else 2


def cmd_simulate(args) -> int:
    from backend.data.simulator import PROFILES, generate_ohlcv

    symbol = args.symbol.upper()
    if symbol not in PROFILES:
        print(f"No profile for {symbol}. Available: {', '.join(sorted(PROFILES))}")
        return 1

    end = date.today()
    start = end - timedelta(days=args.days)
    df = generate_ohlcv(symbol, start, end, args.timeframe,
                        seed=args.seed, vol_multiplier=args.vol_multiplier)

    # Simulated data is namespaced so it can never be confused with, or merged
    # into, a real instrument's history.
    key = f"SIM|{symbol}"
    total = write_candles(key, args.timeframe, df, source="simulated")
    print(f"Simulated {symbol}: {len(df):,} bars stored under {key} ({total:,} total)")
    print(f"  close {df['close'].iloc[0]:,.2f} -> {df['close'].iloc[-1]:,.2f}")
    return 0


def cmd_verify(args) -> int:
    key = args.symbol if "|" in args.symbol else inst.resolve(args.symbol)
    df = read_candles(key, args.timeframe)
    if df.empty:
        print(f"No stored data for {key} {args.timeframe}")
        return 1

    is_mcx = "MCX" in key.upper()
    report = find_gaps(df, key, args.timeframe, mcx=is_mcx)
    print("\n" + report.summary())

    if report.missing_sessions:
        shown = report.missing_sessions[:15]
        print(f"\n  Missing sessions ({len(report.missing_sessions)}):")
        for d in shown:
            print(f"    {d}")
        if len(report.missing_sessions) > len(shown):
            print(f"    ... and {len(report.missing_sessions) - len(shown)} more")

    if report.thin_sessions:
        shown = report.thin_sessions[:15]
        print(f"\n  Thin sessions ({len(report.thin_sessions)}):")
        for d, actual, expected in shown:
            print(f"    {d}  {actual}/{expected} bars")
        if len(report.thin_sessions) > len(shown):
            print(f"    ... and {len(report.thin_sessions) - len(shown)} more")

    print()
    return 0 if report.is_clean else 2


def cmd_import_csv(args) -> int:
    from backend.data.csv_import import import_csv

    key = args.symbol if "|" in args.symbol else inst.resolve(args.symbol)
    total = import_csv(args.path, key, args.timeframe, strict=not args.force)
    print(f"{total:,} bars stored for {key} {args.timeframe}")
    return 0


def cmd_bootstrap(args) -> int:
    """Fill the cache with a sensible starter dataset in one command."""
    from backend.data.cache import get_bars

    symbols = args.symbols or DEFAULT_SYMBOLS
    timeframes = args.timeframes or ["1d", "5m"]
    start = date.today() - timedelta(days=args.days)

    failures = 0
    for symbol in symbols:
        try:
            key = inst.resolve(symbol)
        except KeyError as exc:
            print(f"  {symbol}: {exc}")
            failures += 1
            continue

        for tf in timeframes:
            try:
                bars = get_bars(key, tf, start)
                if bars.empty:
                    print(f"  {symbol:<12} {tf:>4}  no data ({'; '.join(bars.notes)})")
                    failures += 1
                    continue
                print(f"  {symbol:<12} {tf:>4}  {len(bars.frame):>8,} bars")
            except Exception as exc:
                print(f"  {symbol:<12} {tf:>4}  FAILED: {exc}")
                failures += 1

    print(f"\nBootstrap complete, {failures} failure(s)")
    return 1 if failures else 0


# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ingest", description="Trade Terminal data layer")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show auth, instrument and storage status").set_defaults(func=cmd_status)

    sp = sub.add_parser("instruments", help="sync the instrument master")
    sp.add_argument("--exchange", default="complete", help="NSE | BSE | MCX | complete")
    sp.set_defaults(func=cmd_instruments)

    sp = sub.add_parser("fetch", help="fill the cache from Upstox for a range")
    sp.add_argument("symbol")
    sp.add_argument("--timeframe", "-t", default="5m")
    sp.add_argument("--from", dest="from_date", help="YYYY-MM-DD (default: --days ago)")
    sp.add_argument("--to", dest="to_date", help="YYYY-MM-DD (default: last completed session)")
    sp.add_argument("--days", type=int, default=365, help="lookback when --from is not given")
    sp.set_defaults(func=cmd_fetch)

    sp = sub.add_parser(
        "backfill", help="download 1-minute bases for legacy intraday files"
    )
    sp.set_defaults(func=cmd_backfill)

    sp = sub.add_parser("parity", help="compare derived bars with Upstox's native ones")
    sp.add_argument("symbol")
    sp.add_argument("--timeframe", "-t", default="5m")
    sp.add_argument("--days", type=int, default=10)
    sp.add_argument("--tolerance", type=float, default=0.001)
    sp.set_defaults(func=cmd_parity)

    sp = sub.add_parser("simulate", help="generate synthetic candles")
    sp.add_argument("symbol")
    sp.add_argument("--timeframe", "-t", default="5m")
    sp.add_argument("--days", type=int, default=365)
    sp.add_argument("--seed", type=int, default=42)
    sp.add_argument("--vol-multiplier", type=float, default=1.0)
    sp.set_defaults(func=cmd_simulate)

    sp = sub.add_parser("verify", help="check stored data for gaps")
    sp.add_argument("symbol")
    sp.add_argument("--timeframe", "-t", default="5m")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("import-csv", help="import candles from a CSV file")
    sp.add_argument("path")
    sp.add_argument("--symbol", required=True)
    sp.add_argument("--timeframe", "-t", required=True)
    sp.add_argument("--force", action="store_true", help="import despite validation errors")
    sp.set_defaults(func=cmd_import_csv)

    sp = sub.add_parser("bootstrap", help="fetch a starter dataset")
    sp.add_argument("--symbols", nargs="*")
    sp.add_argument("--timeframes", nargs="*")
    sp.add_argument("--days", type=int, default=365)
    sp.set_defaults(func=cmd_bootstrap)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
