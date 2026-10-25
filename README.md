# Trade Terminal

Automated trading terminal for Indian markets: visual strategy definition, backtesting,
paper trading, and live broker execution — all running the same engine.

Implementation plan to production: [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md).
Original build plan: [Terminal Build Order](https://claude.ai/code/artifact/06d623a9-49ab-4ee2-9750-bbc0f8b25532)

## Status

| Phase | Scope | State |
|-------|-------|-------|
| 0 | Foundation, interfaces | Done |
| 1 | Data layer | Done — 1,143 real NIFTY sessions, 100% coverage |
| 2 | Indicator engine | Done — 24 indicators, streaming/vectorized parity to 1e-9 |
| 3 | Strategy engine | Done — JSON schema, conditions, risk, bar-close semantics |
| 4 | Backtest + metrics | Done — real Indian costs, metrics, option resolution |
| 5 | Frontend | Done — FastAPI + React builder, chart, performance, trade log |
| 6 | Paper trading | Done — live feed, journal, crash recovery, dashboard |
| 7 | Live execution | Not started |

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env      # then fill in UPSTOX_API_KEY / UPSTOX_API_SECRET
```

Create the Upstox app at <https://account.upstox.com/developer/apps>.
Redirect URL must be `https://127.0.0.1:8000/auth/upstox/callback`.

## Daily use

Upstox access tokens are **not refreshable** and die every day at 03:30 IST —
*before* the 09:15 open, so an evening token is already dead by the next session.
There is no way around this — it is a broker constraint. Re-authorize each
morning before the open:

```bash
.venv/bin/python -m backend.data.upstox_auth
```

A browser opens; log in, then paste the redirect URL back into the terminal.
The page it lands on will fail to load — that is expected, the authorization
code is in the URL.

## Data layer

```bash
python -m backend.data.ingest status                     # auth, instruments, storage
python -m backend.data.ingest instruments                # sync contract master
python -m backend.data.ingest bootstrap --days 365       # starter dataset
python -m backend.data.ingest fetch NIFTY -t 5m --from 2024-01-01
python -m backend.data.ingest verify NIFTY -t 5m         # gap check
python -m backend.data.ingest simulate NIFTY -t 5m --days 365 --seed 42
python -m backend.data.ingest import-csv data.csv --symbol NIFTY -t 1d
```

`fetch` resumes from what is already stored, so re-running it is cheap and safe.

### Real vs simulated

Real instruments are keyed as Upstox provides them (`NSE_INDEX|Nifty 50`).
Simulated data is namespaced `SIM|NIFTY` and tagged `source="simulated"` in the
coverage table, so it can never be merged into or mistaken for real history.

Simulated data validates strategy *logic*. It is not evidence of profitability —
Black-Scholes prices on a GBM underlying have no spread, no smile, and no
liquidity holes, so any strategy that harvests that consistency will look
brilliant here and lose money live.

### Upstox data limits

- 1-minute and hourly candles: from **January 2022**
- Daily, weekly, monthly: from **January 2000**
- Rate limits: 50 req/s, 500 req/min, 2000 req/30min (handled automatically)
- Per-request range is capped, shrinking as the interval shrinks — `fetch_candles`
  chunks and stitches transparently

## Paper trading

Paper runs the **same** `StrategyEngine` as the backtest. The only things that
change are where bars come from and where orders go:

```
backtest -> HistoricalFeed + SimulatedBroker
paper    -> LiveFeed       + SimulatedBroker
live     -> LiveFeed       + UpstoxBroker      (Phase 7)
```

`test_live.py` asserts that a paper session and a backtest over the same bars
produce identical trades. If that ever fails, backtest results have stopped
describing the system that actually runs.

```bash
# during market hours, against the live Upstox feed
python -m backend.live.run paper --symbol BANKNIFTY -t 5m --lot-size 15

# any time: the identical live path over stored candles
python -m backend.live.run replay --symbol BANKNIFTY -t 5m \
       --from 2026-06-01 --to 2026-08-11 --lot-size 15 --speed 60

python -m backend.live.run sessions           # what has run
python -m backend.live.run show <session-id>  # one session in detail
```

Or drive it from the **Live** tab in the UI, which polls a running session and
can start and stop one.

### Bars, not ticks

The default live feed polls Upstox's intraday-candle endpoint rather than
aggregating the tick socket. That is a correctness choice: those candles come
from the same pipeline that produced the historical candles the backtest read,
so paper and backtest see identical bars. Building bars from ticks ourselves
would be a second bar-construction implementation, and a second implementation
is a second set of bugs that surface only as an unexplained gap between
backtest and live.

The WebSocket path (`backend/live/websocket.py`, protobuf, auto-reconnect) is
built and ready for Phase 7, where a stop has to trigger inside a bar rather
than at its close. Regenerate the decoder after updating the schema:

```bash
protoc --python_out=backend/live/proto --proto_path=backend/live/proto \
       MarketDataFeedV3.proto
```

### Crash recovery

Every bar, signal, order and fill is appended to `data/journal/<session>.jsonl`
as it happens. Money events are `fsync`ed before the call returns.

Restarting a session on the same strategy, instrument and day lands on the same
journal and resumes: positions come back with their original stops, the bar
index continues, daily risk counters are **not** reset, and orders already
filled cannot fill again. Verified by `kill -9` mid-session against a year of
real BANKNIFTY data — the resumed run reproduced the uninterrupted run's 71
trades and P&L exactly, with zero duplicate fills and zero replayed bars.

A torn final line is the expected signature of a crash, not corruption; the
reader takes the intact prefix and says so.


## Layout

```
backend/
  core/
    interfaces.py     DataFeed + Broker protocols — the seam everything hangs off
    config.py         .env loading; the only place secrets are read
  data/
    upstox_auth.py    OAuth, token cache, expiry health check
    upstox_client.py  REST client: rate limiting, chunking, retries
    instruments.py    contract master sync and lookups
    store.py          DuckDB metadata + Parquet candles
    calendar.py       trading sessions, holidays, gap detection
    simulator.py      GBM + jump-diffusion, Black-Scholes, Greeks
    csv_import.py     external CSV ingestion with validation
    ingest.py         CLI front door
  live/
    session.py        the paper/live loop — mirrors run_backtest exactly
    feed.py           DataFeed over a live source, history-backed warm-up
    sources.py        REST polling, tick aggregation, replay
    websocket.py      Upstox V3 socket: protobuf, reconnect (Phase 7)
    aggregator.py     ticks -> bars, clock-driven close, gap fill
    broker.py         SimulatedBroker: idempotent fills, independent ledger
    journal.py        append-only event log and recovery
    manager.py        one thread per session, for the API
    alerts.py         Telegram
    run.py            CLI front door
  tests/
data/
  parquet/            candle files (gitignored)
  journal/            live event journal (gitignored)
  terminal.duckdb     instruments, coverage, metadata (gitignored)
```

## Running the app

Two processes. Backend first:

```bash
.venv/bin/uvicorn backend.api.main:app --reload --port 8000
```

Then the UI (proxies /api to port 8000):

```bash
cd frontend && npm run dev        # http://localhost:5173
```

## Tests

```bash
.venv/bin/python -m pytest backend/tests -q
```

No network or broker token required — the suite runs against the simulator.

**Stop the API server first.** DuckDB permits a single writer process, so the
suite cannot open the database while uvicorn holds it:

```bash
pkill -f 'uvicorn backend.api.main'
```

## Regulatory

Single user trading own capital, under 10 orders/second: no algo registration
required under SEBI's framework. A **static IP registered with the broker is
mandatory for order placement** (market data works from any IP). Multi-user
changes this completely — broker partnership, exchange-assigned Algo IDs, and
per-user IP routing all become required.

Not SEBI registered. Nothing here is investment advice.
