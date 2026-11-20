# Trade Terminal: production readiness plan

Prepared 2026-10-01 from three inputs: the *Trade-Terminal: local-first agentic trading platform* plan (Claude Docs artifact), the older *Terminal Build Order* artifact, and an audit of this repo. For the audit I read the code, ran the 357 backend tests (all pass, 47 s), ran `tsc` (clean), built the UI, and ran `npm audit` (0 vulnerabilities). A Python dependency audit was not run.

Item IDs (P0.1, P1.2, ...) are stable, so you can say "start P0" or "drop P3.4".

---

## Status

| Phase | State | Notes |
|---|---|---|
| **0** Stabilise the base | **Done**, merged to main 2026-11-18 (PR #2) | P0.1 to P0.8. See below for what is left open. |
| **1** Vertical slice | **Done**, merged 2026-11-18 (PR #3). M1 gate partly verified, see below | P1.1 to P1.6 |
| **2** Measure and harden | **In progress**. P2.1, P2.4, P2.5, P2.7, P2.8 merged 2026-11-18 (PR #4); P2.2 part 1 on `phase-2-schema-v2` | Open: rest of P2.2, P2.3, P2.6, P2.9 |
| **3** Screener and MCP | **Started** 2026-11-19 at the owner's direction, before a trader confirmed "screener". Working definition: the trigger-line watchlist the owner shared (status per stock, when it fired, distance to the line). P3.1 scan core, Screener UI (merged 2026-11-20, PR #7), saved scans and alerts (PR #8), P3.3 prefetch, P3.4 scans from plain English, P3.5 MCP server | Open: rest of P3.1, P3.2 (needs the token), MCP with a second client model |
| **4** Research agent and portfolios | **Started** 2026-11-20. P4.2 part 1 (honest Sharpe, deflated Sharpe) first, because the research graph's gate depends on it | Open: rest of P4.2, P4.1, P4.3, P4.4 |
| 5 | Not started | |

So far (2026-11-20): 800 backend tests, 20 component tests, three Playwright specs (the chat happy path, a Builder sweep, the screener), which also run against the installed wheel.

- **P2.1** has the set (50 cases, 16 holdout) and the harness, but **no model has been run against it**: there is no Anthropic key on this machine yet. The gold specs were written by the assistant and need a trader's review before any number from them is quoted.
- **P2.2 part 1 (done):** schema version 2, a strict superset of v1. A document records the lowest version that expresses it, so every v1 document keeps its hash, id and family (golden test over all templates). Added expression operands (rolling `rank` and `quantile` over the previous N bars; `add`, `sub`, `mul`, `div`), with vectorized and streaming agreeing to 1e-9 and a paper-equals-backtest gate test; paper warm-up extends by the longest window. Added `sweep` (range parameters): `POST /api/jobs/sweep` runs every variant as a recorded backtest, so the grid counts toward the family's trials; invalid variants are skipped with a reason, and no "best" row is named. No v1-to-v2 migration step is needed because v1 documents are valid v2 documents.
- **P2.2 part 2 (open):** regime-conditional parameters and VIX- or IV-scaled thresholds wait on the "not point to point" answer and an India VIX series; risk-based sizing needs a design for option legs, whose premium is unknown when the entry signal fires. Sweeps now run from the UI and the chat builder too: a Sweep panel in the Builder, a results table that lists every variant in grid order with the spread and the family's trial count, and in the assistant an approval that names the number of backtests, a grid explained by code (not the model, so no winner is named), progress and cancel per variant, and dropping the sweep when the trader picks values.
- **P3.1 part 1 (done):** a scan is the strategy condition language run over a universe on daily bars (`backend/scan`). A universe is a list of instruments or the `fno_stocks` preset (stock underlyings of NSE futures in the instrument master, today's list, labelled as such). *Scan now* gives each stock a status (triggered today, holding, lapsed, waiting), when it began and the close then, when it last held, and, for a single comparison, the distance past or short of the line. *Scan history* lists every onset with 1, 5 and 20-bar forward returns from the next open, pooled and set against the same stocks' unconditional forward returns. Every instrument is in the result, with the reason when it could not be scanned, and stale data is flagged. `POST /api/scans/validate`, `POST /api/jobs/scan`. Gate progress: three scans checked against independent pandas calculations (SMA level, prior-day-high cross through the shared context path, an arithmetic threshold in history mode). A Screener tab in the UI builds scans with the Builder's panels and shows both modes (Playwright spec). Found and fixed on the way: reads on the shared DuckDB connection raced across threads (the UI requests datasets twice on load); every read now runs on its own cursor, closed when fetched.
- **P3.1 saved scans and alerts (done):** scans are saved and versioned like strategies. A saved scan with alerts on is checked at `SCAN_ALERT_TIMES` (default 08:45 IST, because the history endpoint serves a session's daily bar only the next day). A check compares every stock with the previous snapshot and reports only changes (triggered, back inside); missing data is not a change; the first check, or the first after the spec changes, records a baseline and sends one summary; a check with no new session records nothing, so catching up after downtime sends nothing twice. Changes go to Telegram when it is configured and are listed in the Screener either way. Same-evening alerts need today's bar built from intraday data, which waits on the Upstox token.
- **P3.1 still open:** a scan feeding a strategy's universe (needs the Phase 4 portfolio engine, or a batch backtest over the hits); the "against Nifty" relative-strength view from the owner's screenshot (needs a benchmark operand).
- **P3.3 (done, untested against the live API):** `backend/data/prefetch.py` plans a universe's download before it runs (per instrument: sessions the cache cannot vouch for, the requests they need) and estimates the time from the three rate limits, naming the one that binds (8,800 one-minute requests: over two hours, as estimated above). The prefetch job runs `cache.ensure` per instrument, so the ledger makes it resumable: `POST /api/jobs/{id}/resume` reruns a stopped download and skips what is stored. Progress reports requests done and time left (planned, or measured when slower); a cancel takes effect within one request; one failing instrument does not stop the rest. The Screener's universe panel has Check history, Download and Resume. Live intraday scans still wait for the WebSocket path.
- **P3.4 (done, scripted model only):** `backend/agent/scan_agent.py` turns a description into a scan spec with the builder's discipline: data only from the model, at most 2 questions (enforced in code), at most 3 repairs, a read-back rendered from the validated spec, and changes as JSON patches. Compiling never runs a scan (the module cannot import the scan runner; a test checks it); the trader runs it from the read-back. Stateless: the client carries the dialogue, so there is nothing to checkpoint. The Screener has Describe a scan, with questions, read-back, Use, Use and scan now, and Change. Results with charts are not built yet, and no eval set covers scans; both wait until a real model can be measured.
- **P3.5 (done):** `trade-terminal mcp` serves ten tools over stdio (`backend/mcpserver`): the strategy and scan languages, validate (with the code read-back), run backtest and run scan (job ids), job status, cancel, get report (an HTML file on disk plus headline numbers, not raw trades) and list strategies. It is a thin client of the running app's API, because DuckDB allows one writer process, and each tool may call only an allowlisted route; tests check the allowlist, that the package imports nothing from the execution or live layers, and that the tool list is fixed. Gate: compile (the client writes and validates the document), backtest, report and cancel completed over a real stdio session against a running app (protocol 2025-11-25); a stdio session is in the test suite. Not yet: a session from Claude Desktop itself and a second client model, which need the desktop app and a key.
- **P4.2 part 1 (done):** Sharpe, Sortino and volatility now come from daily returns (session closes) with no risk-free rate, because the backtest credits no interest on idle cash; before, a mostly idle strategy showed Sharpe values like -54, and the same strategy's Sharpe depended on its bar size. Every backtest reports its probabilistic Sharpe ratio and its deflated Sharpe ratio (Bailey and López de Prado) against the expected best of N tries, where N is the family's distinct experiments (strategy version and data set pairs; identical reruns do not count). Shown in the Performance tab, the chat's result card and explanation, the HTML report and MCP. A test takes the best of 200 pure-noise strategies: it passes on its own (PSR above 95%) and fails after deflation. Open in P4.2: the final holdout, walk-forward, neighbourhood robustness for swept parameters, slippage stress, rerun on a second instrument.
- **P2.3** (real option premiums) and the M1 broker checks wait on a fresh Upstox token; **P2.6** on the data-terms answer.
- Found while writing the eval's gold specs, and fixed: `max_trades_per_day` and the re-entry cap counted legs instead of entries (multi-leg results change); the builder told the model to write stock keys that do not exist.

Phase 1, what exists: read-through bar cache (1-minute and daily bases, derived timeframes, session ledger, fingerprints); SQLite app state with versioned strategies, run history and a job runner with progress and cancel; an LLM layer (Claude via the official SDK, keychain keys, per-turn caps); the LangGraph builder (at most 2 questions, at most 3 repairs, approval before any backtest, code-rendered read-back, JSON-patch refinements, number-checked explanations, SQLite checkpoints); the Assistant and Library UI with one-click paper trading.

M1 gate, verified: describe, approve, backtest without a download step, open the report, start paper (Playwright, real stack, scripted model); restart resumes at the same pause and a cut-off turn continues without redoing work (graph tests); the agent cannot import the order path (AST and transitive import tests); parity, crash-recovery and lookahead gates still green; the agent's backtest equals the HTTP endpoint's.

M1 gate, not yet verified (needs your credentials and spend):

- **The 10-description smoke check against the real model.** No Claude call has been made; every agent test uses a scripted model.
- **The broker path.** The Upstox token in `.env` expired in August, so live downloads and `ingest parity` (derived vs native 5m bars) have not run against Upstox.
- **A clean-machine install.** Packaging is P2.4.

Phase 0 gate, as run locally: the suite leaves `data/` byte-identical (hash before and after); `uvicorn` shows application `INFO` lines and writes `data/logs/terminal.log`; SIGTERM ends a running session with a `shutdown` end record in 0.3 s; after `kill -9` the restart reports the session as interrupted. The CI workflow exists but has not yet run on GitHub.

Bugs found and fixed while doing Phase 0 (none were in the audit):

- `read_coverage` raised without the optional `pytz` package, which broke the ingest CLI's resume path.
- A paced replay slept uninterruptibly between bars, so stop and shutdown could not end it.
- A stop arriving while the feed was waiting was recorded as `feed_exhausted`, which flattened positions and misreported the reason.
- A session stopped and restarted on the same day, then crashed, was read as cleanly ended: not reported as interrupted, and its open position would have been forgotten on the next start.

Left open from Phase 0:

- **mypy** runs in CI as informational; 29 existing type errors in engine and data code are not yet cleared.
- **Startup resume prompt (P0.4):** done in P1.5: a banner reports interrupted sessions and links to the Live tab; nothing resumes silently.
- **Test leftovers in `data/`** (`SIM__APITEST` files and three `*-SIM-APITEST-*.jsonl` journals) are still there, awaiting confirmation to delete.
- Replay with no `replay_start` ends immediately with zero bars (the whole store is treated as warm-up). The UI's one-click paper start always sets a start now; the API default still behaves this way.

---

## 1. Verdict

The engine is production-grade in the ways that are hardest to retrofit: one engine for backtest and paper (with a parity test), bar-close semantics, Indian cost model, an fsync'd journal with `kill -9` recovery, strict content-hashed strategy schema. **Keep it; do not rewrite it.**

Everything around the engine is still prototype-grade. There is no agent layer, no data cache, no saved strategies or results, no job runner, no logging config for the API, no CI, no packaging, and the tests write into the real `data/` folder. The largest credibility gap is that option premiums are Black-Scholes estimates, not market prices.

Nothing in the repo can place a real order today. `SimulatedBroker` is the only `Broker`, and the API hard-codes paper mode. That is the right state to be in until Phase 5.

### What "production-ready" should mean

The source plan splits features into MVP, Version 2 and Last. I read "production-ready" as four milestones, and recommend **M2** as the target for the word. Live trading is a separate, gated decision.

| Milestone | Ends | You can say |
|---|---|---|
| **M1 Slice works** | Phase 1 | Describe a strategy in English, approve the read-back, get a backtest, click paper trade, with no download step. Demo and resume ready. |
| **M2 MVP beta** | Phase 2 | M1 plus measured compile accuracy, real option premiums (or honestly labelled model ones), hardened, installable by someone else. Safe to hand to 3 to 5 traders. **Recommended meaning of "production-ready".** |
| **M3 Version 2** | Phase 4 | Screener, MCP server, research agent, portfolio strategies, advanced validation. |
| **M4 Live-capable** | Phase 5 | Real orders under approvals, risk limits and a kill switch, after the regulatory check. |

---

## 2. Audit findings

Severity is what it costs if left alone. "Lands in" is the item that fixes it.

| # | Finding | Evidence | Severity | Lands in |
|---|---|---|---|---|
| 1 | **Tests write into the real data folder.** Settings are created at import and make `data/` directories; `test_api.py` seeds `SIM\|APITEST` into the live store. Running the suite today left a 6 MB journal in `data/journal/` and touched `terminal.duckdb`. It also forces the "stop the server before testing" rule, because DuckDB allows one writer. | [config.py:104](backend/core/config.py:104), [test_api.py:38](backend/tests/test_api.py:38) | High | P0.1 |
| 2 | **Candle writes are not atomic or locked.** `to_parquet` writes straight over the live file, and `write_candles` is read-merge-write with no lock. A crash mid-write corrupts a file; two concurrent fetches lose one. | [store.py:175](backend/data/store.py:175) | High | P0.2 |
| 3 | **The API process never configures logging.** 24 modules log, but only the CLIs call `basicConfig`, and `LOG_LEVEL` is read only by the live CLI. Under `uvicorn`, app `INFO` lines (including paper-session lines) are dropped. | [run.py:241](backend/live/run.py:241) | High | P0.3 |
| 4 | **Sessions are not stopped on API shutdown.** `SessionManager.stop_all` exists but nothing calls it, and there is no startup prompt for interrupted sessions. | [manager.py:90](backend/live/manager.py:90) | Medium | P0.4 |
| 5 | **No CI, no lint run, no Python lockfile.** Ruff is configured but not installed. Runtime is Python 3.14.3 with pandas 3.0 and numpy 2.5 (very new majors), unpinned. The repo has one commit. | [pyproject.toml:28](pyproject.toml:28) | Medium | P0.5 |
| 6 | **Local API has no Host/Origin check, and long work is synchronous.** Fetch and backtest run inside request threads with no job id, progress or cancel. | [main.py:248](backend/api/main.py:248), [main.py:381](backend/api/main.py:381) | Medium (High once remote) | P0.8, P1.2, P5.6 |
| 7 | **User work is not saved.** The builder says "save it as your own", but there is no save endpoint and no run history. Only 12 templates and 3 JSON files exist. | route list in [main.py](backend/api/main.py) | Gap | P1.2 |
| 8 | **No read-through data cache.** One file per instrument and timeframe, a fixed-year download from the UI, and a 404 when data is missing. | [main.py:90](backend/api/main.py:90), [Builder.tsx:286](frontend/src/Builder.tsx:286) | Gap (planned) | P1.1 |
| 9 | **Option premiums are model prices.** Black-Scholes on the underlying's realized vol: no spread, smile or liquidity holes. The module's own docstring says so. | [option_book.py:1](backend/strategy/option_book.py:1) | Gap, top credibility risk | P2.3 |
| 10 | **Account kill switch counts realized P&L only.** An open short option losing heavily does not trip the account limit until it closes. Fine for paper, unacceptable live. | [risk.py:170](backend/strategy/risk.py:170), [risk.py:219](backend/strategy/risk.py:219) | High before live | P5.2 |
| 11 | **Telegram messages are HTML but names are not escaped.** LLM-authored strategy names containing `<` will make sends fail silently. | [alerts.py:63](backend/live/alerts.py:63) | Low | P0.7 |
| 12 | **Secrets in plaintext, loose permissions.** `.env` is mode 644 and holds the Upstox secret and a pasted access token. History is clean (it was never committed) and the token cache is chmod 600. LLM keys will be added to this problem in Phase 1. | `ls -l .env` | Medium | P0.6, P1.3, P2.5 |
| 13 | **No packaging.** The comment at [main.py:43](backend/api/main.py:43) says the UI is served from the API in production, but nothing mounts it. No console entry point, no installer. | | Gap | P2.4 |
| 14 | **Frontend has no tests.** | [package.json](frontend/package.json) | Low | P1.5 |

**Keep as is:** engine parity gate, crash recovery, cost model, schema strictness, the `DataFeed` and `Broker` seam, all 357 tests, `tsc` and Vite build, clean `npm audit`.

---

## 3. Roadmap at a glance

Estimates are rough part-time weeks at the pace the source plan assumed (it scheduled only the slice, at 4 to 6 weeks). Re-estimate at every gate. Phases run in order; a gate must pass before the next phase starts.

| Phase | Goal | Est. weeks | Gate to finish | Milestone |
|---|---|---|---|---|
| **0** Stabilise the base | Fix what would make later failures hard to diagnose | 1 | Tests leave `data/` untouched, CI green, logs visible | |
| **1** Vertical slice | English to strategy to approved read-back to backtest to paper, no download step | 4 to 6 | Clean-machine end-to-end run, restart resumes at the pause, agent cannot reach the order path | **M1** |
| **2** Measure and harden | Eval set, schema v2, real option data, packaging, security, private beta | 4 to 6 | Published eval numbers, second-machine install, 3 beta traders | **M2** |
| **3** Screener and MCP | The screener mode traders actually meant, plus a local MCP server | 3 to 5 | Screener meaning confirmed *before* start; hand-verified scans | |
| **4** Research and portfolio | Variant research with honest selection, momentum-rotation strategies | 5 to 7 | Zero survivors on pure-noise data after deflation | **M3** |
| **5** Live trading | Real orders behind approvals, MTM kill switch, reconciliation | 6+ | Regulatory check and your explicit sign-off *before* any order code merges | **M4** |

Phases 0 to 2 are about 9 to 13 weeks. They give you the resume-grade slice and a production-ready MVP, and they are the part to protect during placement season.

### Work distribution by track

| Track | P0 | P1 | P2 | P3 | P4 | P5 |
|---|---|---|---|---|---|---|
| **Data** | atomic writes | read-through cache, derived timeframes | real option chain, partitioned storage, VIX series | universe tables, index membership | survivorship-safe universes | WebSocket feed for in-bar stops |
| **Engine and schema** | | | schema v2 and migrator, trial counter | multi-instrument evaluation | portfolio engine, walk-forward, holdout lock | MTM kill switch, `UpstoxBroker` |
| **Agent** | | builder graph, LLM adapter, approvals | eval set and harness | screener graph, MCP tools | research graph (`Send`), judge, memory | |
| **Platform** | lifecycle hooks, local API hardening | app-state DB, job runner | migrations, backups | MCP job tools | parallel backtests | remote auth and TLS |
| **Frontend** | | chat, read-back card, saved strategies, report | wizard, diagnostics, labelling | scan results | research report | kill-switch button, live dashboard |
| **Quality** | test isolation, CI, lint | fake-LLM graph tests, UI smoke | eval gate, schema property tests | scan correctness | false-discovery test | broker contract tests, failure drills |
| **Security and ops** | logging, redaction, `.env` perms | keychain for LLM keys | keychain for broker secrets, `doctor` | MCP has no order tools | experiment ledger | heartbeat, daily reconciliation |
| **Compliance** | | | disclaimers, data-terms check | index-membership caveats | multiple-testing explainer | regulatory check, sign-off |

---

## 4. Phases in detail

### Phase 0: Stabilise the base (about 1 week)

You are about to put an LLM and a data cache on top of this. Fix the things that turn later bugs into mysteries.

- **P0.1 Isolate tests.** Add `backend/tests/conftest.py` that points `DATA_DIR` at a temp folder *before* `backend.core.config` is imported, plus a guard that fails the run if the data dir is the repo's `data/`. Drop the "stop the server first" rule from the README. Then, after you confirm, delete the test leftovers already in `data/` (`SIM__APITEST` parquet files and the two `*-SIM-APITEST-*.jsonl` journals).
- **P0.2 Atomic, locked candle writes.** Write to a temp file and `os.replace`; take a per-instrument lock around read-merge-write.
- **P0.3 One logging setup.** A single `configure_logging()` called from API startup and the CLIs; honours `LOG_LEVEL`; rotating file under `data/logs/`; a redaction filter. **Redaction is required, not optional:** the Telegram bot token sits in the request URL, and `httpx` logs request URLs at `INFO`, so turning logging on without redaction would write the token to disk.
- **P0.4 Lifecycle.** FastAPI lifespan: call `manager.stop_all("shutdown")` on exit; on startup, list interrupted sessions and let the user choose to resume (never resume silently).
- **P0.5 Tooling.** Add ruff (and a lenient mypy) to the dev extras; lock Python dependencies (`uv lock` or pip-tools); GitHub Actions running pytest, ruff, `tsc`, `vite build`, `npm audit` and `pip-audit`. Work on feature branches from here on.
- **P0.6 Hygiene.** `chmod 600 .env`; point the README at the new plan; align versions (pyproject says 0.1.0, API and UI say 0.5.0).
- **P0.7 Escape Telegram HTML** in strategy names and error text.
- **P0.8 Harden the local API.** Bind to `127.0.0.1` in the documented command; reject requests whose `Host` or `Origin` is not localhost (blocks DNS-rebinding and drive-by POSTs to `/api/live/start`).

**Gate:** `pytest` passes with a before-and-after hash showing `data/` unchanged; CI is green on a fresh clone; running `uvicorn` shows application `INFO` lines; killing and restarting the API leaves the journal consistent.

### Phase 1: Vertical slice (4 to 6 weeks) → M1

Goal, from the source plan: describe a strategy in English, get a validated spec, approve a read-back, backtest, report, and start paper trading in one click, with no download step.

- **P1.1 Read-through data cache.** One function, `get_bars(instrument, timeframe, start, end)`; everything asks it. Rules from the plan: download only 1-minute (intraday) or daily bars and derive 3m, 5m, 15m and 1h locally, bucketed from 09:15; fetch only missing sessions, merged into a few ranges; past bars are permanent and only the last few days are re-checked; remember sessions with no data (holidays, pre-listing). Add a parity test that derived 5m bars equal Upstox's native 5m on data you already have. Store a fingerprint (range plus hash) with every result. Retention mode is "keep" only for now (see P2.6). Includes a migration path for the per-timeframe files already on disk.
- **P1.2 Persistence and jobs.** App state (strategies versioned by content hash, runs and their fingerprints, conversations, LangGraph checkpoints) goes in SQLite with WAL. DuckDB stays for analytics over Parquet; its single-writer limit is a poor fit for app state. Add strategy CRUD with version history and a run history. Add a background job runner (job id, progress, cancel) for fetch, backtest and sweeps; the MCP server in Phase 3 reuses it.
- **P1.3 LLM layer.** A thin provider adapter (Anthropic, OpenAI, Gemini; Ollama later) producing structured output against the `Strategy` JSON schema. Keys live in the OS keychain, never in `.env` or logs. Hard caps on tokens, steps and wall-clock per compile, with a visible counter.
- **P1.4 Builder graph (LangGraph).** Nodes: understand, clarify, compile, validate, read-back, backtest, explain, refine.
  - **Clarify** asks at most two blocking questions (`interrupt()`); every other default is listed as an assumption.
  - **Validate** feeds pydantic errors back to the model, at most 3 repair loops.
  - **Read-back** is the approval pause; no backtest runs before it.
  - **Refine** applies each follow-up as a patch; the content hash gives version history.
  - SQLite checkpointer so a paused graph survives a restart.
  - **Structural guardrail:** `backend/agent` may call engine tools but must not import anything under `backend.live` that can place orders. Enforce it with an import test, because "language models never sit in the order path" should be a failing test, not a convention.
  - Treat pasted text (video transcripts, posts) as untrusted input. Tools are compute-only; the model receives computed numbers and may not invent metrics in the explanation.
- **P1.5 Frontend.** Chat panel, read-back card with approve and edit, saved strategies and version history, result report, one-click "start paper", data-progress toasts. Keep the "synthetic data" badge. Add Vitest for components and one Playwright happy path.
- **P1.6 Quality.** Graph tests with a deterministic fake LLM (interrupts, repair loop, resume after restart). Extend the existing parity gate: a backtest run through the agent tool equals the same run through the HTTP endpoint.

**Gate (M1):** on a clean data dir, from install to a running paper replay, you can connect the broker, type a strategy, answer at most two questions, approve the read-back, see a backtest without manually downloading data, and start paper. Restarting the API mid-graph resumes at the same pause. The import test proves the agent cannot reach order placement. Every existing gate (parity, crash recovery, lookahead) is still green. As a smoke check, 10 hand-picked descriptions compile without JSON edits; the formal accuracy number comes in Phase 2.

### Phase 2: Measure and harden for beta (4 to 6 weeks) → M2

- **P2.1 Evaluation set and harness.** 50 to 100 plain-English strategies with hand-written correct specs, including ambiguous and Hinglish or trader-jargon cases. Score exact match per field (entry, exit, legs, risk), valid-spec rate, questions asked, repair loops and cost per compile. Split dev and holdout (about 70/30) so prompt tuning cannot overfit. CI runs a small subset against recorded responses; the full run is manual or nightly. Quote X% and Y strategies on a resume only after this exists.
- **P2.2 Schema v2.** The four extensions from the plan: expression-valued thresholds (percentile ranks, VIX- or IV-scaled values), regime-conditional parameters, range parameters the backtester sweeps, and risk-based sizing. Bump `schema_version`, write a v1-to-v2 migrator, keep v1 documents loadable so old results reproduce. Streaming and vectorized evaluation must still agree to 1e-9 for the new expression types. Needs an India VIX series.
- **P2.3 Real option premiums.** This is the largest credibility fix.
  - *Spike first (2 to 3 days):* Upstox publishes **Expired Instruments** APIs (expiries, expired option and future contracts, expired historical candles) on the **Upstox Plus plan**. Their announcement does not state price or history range, so confirm both before committing. Sources are listed at the end.
  - *If viable:* ingest strike-wise 1-minute candles for expired NIFTY and BANKNIFTY contracts; switch to Parquet partitioned by underlying, expiry and strike (one file per contract will not scale); add a real-chain provider to `OptionBook` that prefers market premiums and falls back to Black-Scholes.
  - *Either way:* label each leg's premium source ("market" or "model") and report the share of fills priced from market data. Compare model against market premiums on contracts where both exist and publish that error.
  - *If Plus is not worth it:* start recording live option-chain snapshots now (the original build order's idea) and keep the model, loudly labelled.
- **P2.4 Packaging and first run.** Mount the built UI from FastAPI; add a `trade-terminal` console entry point; install via `pipx` or `uv tool` (a desktop wrapper can wait until after Phase 3). First-run wizard: data directory, Upstox app creation steps, one-click morning login. Upstox requires an `https` redirect even for localhost; keep the paste-the-URL flow for beta and revisit.
- **P2.5 Security.** Broker secrets and tokens move to the OS keychain, with `.env` as an explicit fallback. Security checklist: file modes, log redaction test, dependency audit, CORS and Origin tests.
- **P2.6 Data retention setting.** Session, keep and hybrid modes from the plan, shipped only after the Upstox data-terms question is answered. Until then "keep" is the default.
- **P2.7 Operations.** `trade-terminal doctor` (token status, instrument-master age, coverage, disk, versions) and a redacted diagnostics bundle. Daily backup of strategies, runs and journals (market data is regenerable, so excluded). Versioned database migrations and a version check at startup.
- **P2.8 Trial counter.** Record every backtest per strategy family now so deflated Sharpe is possible in Phase 4. It is cheap today and impossible to reconstruct later.
- **P2.9 Private beta.** 3 to 5 traders. Ask them the two unresolved feedback questions (what "not point to point" meant; what "screener" meant). Their answers gate Phase 3.

**Gate (M2):** eval report published with holdout numbers per model; real-premium coverage reported for NIFTY and BANKNIFTY (or the decision to ship model premiums, labelled, recorded); someone other than you installs it on a second machine in under 15 minutes; 3 beta traders each run at least 5 strategies with no data loss; a crash and restart drill passes; security checklist complete.

### Phase 3: Screener and MCP (3 to 5 weeks)

**Do not start until a trader has confirmed what "screener" meant.** Build that mode first.

- **P3.1 Multi-instrument evaluation.** Run the condition language across a universe. Daily-bar scans first (cheap). Modes: scan now, scan history with 1, 5 and 20-day forward returns, scan feeds a strategy's universe, scan to alerts (Telegram alerting already exists).
- **P3.2 Survivorship and adjustments.** Use historical index membership, not today's list, or label results. **Verify** whether Upstox daily candles are adjusted for splits and bonuses; screener and momentum results are wrong if they are not.
- **P3.3 Rate-limit planning.** 200 stocks of intraday data is about 8,800 requests against 2,000 per 30 minutes, which is over two hours. Use a resumable prefetch job with an ETA; live intraday scans wait for the WebSocket path.
- **P3.4 Screener graph.** Plain English to scan spec, read-back, run, results with charts; approval before a scan becomes a strategy universe.
- **P3.5 Local MCP server (stdio).** Tools: validate strategy, run backtest (returns a job id), job status, get report (path to HTML, not raw trades), list strategies, run scan. No tool can place orders. Test with at least two client models.

**Gate:** three scans hand-verified against an independent calculation; a Claude Desktop session completes compile, backtest and report through MCP; job cancel works.

### Phase 4: Research agent and portfolio strategies (5 to 7 weeks) → M3

- **P4.1 Portfolio engine on daily bars.** Universe, rank, rebalance and regime filters (momentum rotation), behind the same `DataFeed` and `Broker` seam, with a parity test against paper.
- **P4.2 Advanced validation.** Walk-forward, parameter-neighbourhood sweeps for range parameters, slippage stress heatmap, rerun on a second instrument, deflated Sharpe using the Phase 2 trial counts, and a **final holdout the engine lets a strategy family touch once**.
- **P4.3 Research graph.** `Send` fans out *structural* variants; a variant that only changes a numeric literal is rejected by a diff check. A judge ranks by robustness. Approval pauses cover the plan and its cost, and which survivors to keep. A research memory records which ideas failed on which instrument and regime. Show an estimated cost before approval and enforce a hard cap.
- **P4.4 Throughput.** Process-pool backtests with fixed seeds, results written to SQLite (not the single-writer DuckDB).

**Gate:** run the research graph on pure-noise simulated data (the repo already has a simulator). After deflation it must report zero survivors above the agreed threshold. That is the direct test of the plan's multiple-testing concern.

### Phase 5: Live trading (6+ weeks, gated) → M4

**Entry gates, both required:** (1) the regulatory check is done, covering tool-versus-provider status, Algo-ID handling and static IP, because the plan's SEBI notes come from secondary sources; (2) you give explicit go-live sign-off. No real-order code merges before both are ticked.

- **P5.1 `UpstoxBroker`.** Place, modify, cancel; order updates over the portfolio stream; partial fills and rejects; product type; freeze-quantity slicing for index options (verify the current limits). Idempotency through the client tag (verify Upstox's tag length and uniqueness rules). Reconcile against the broker's positions and orders at start and after every reconnect, and refuse to trade on a mismatch.
- **P5.2 Account kill switch on mark-to-market.** Change [risk.py:170](backend/strategy/risk.py:170) and [risk.py:219](backend/strategy/risk.py:219) to count unrealized loss, with tests, plus max order size and notional, a fat-finger price band, an order-rate cap under 10 per second, and a server-side flatten-all.
- **P5.3 Independent watchdog.** A second process that can flatten positions and cancel orders if the trading process dies or goes silent.
- **P5.4 WebSocket feed** wired in (the V3 protobuf decoder exists) so a stop can trigger inside a bar; stale-feed detection pauses trading and alerts.
- **P5.5 Progression.** Paper, then shadow-live (orders generated, not sent, compared with paper), then **1 lot, 1 strategy for 2 weeks**, then scale. The original plan's paper gate stands: at least 20 sessions with drift from the equivalent backtest under an agreed threshold, defined at the start of this phase.
- **P5.6 Deployment profile.** Live orders need a static IP registered with the broker, so decide between a Mumbai VPS (the old plan estimated ₹400 to 900 a month) and a home static IP. A remote deployment makes API authentication and TLS mandatory; this is where that work lands.
- **P5.7 Daily token handling.** Tokens cannot be refreshed and die at 03:30 IST. Send a morning reminder, offer one-click login, and fail closed: no valid token at the open means no orders and an alert.
- **P5.8 Typed confirmation** for paper to live, an immutable order audit trail (the journal covers most of it), and the compliance copy in the UI.
- **P5.9 Failure drills:** `kill -9` (already proven for paper), 60-second network cut, broker 5xx, duplicate-tag resend, clock skew, special sessions (Muhurat), expiry day.

**Gate:** all drills pass; a daily reconciliation report runs clean; 20 shadow sessions with order-level agreement; then the 2-week one-lot run.

---

## 5. Decisions needed from you

1. **Target:** is "production-ready" M2 (my recommendation) or M4?
2. **Upstox Plus** for expired option data: worth the cost? Price and history range are unconfirmed; the P2.3 spike answers it.
3. **App-state store:** SQLite for state, DuckDB for analytics (my recommendation), or keep everything in DuckDB.
4. **Default LLM provider** for the builder. Let the Phase 2 eval decide.
5. **Hours per week.** All estimates assume the source plan's part-time pace.

**Open questions inherited from the plan** (each gates work above):

- What did "constraints should not be point to point" mean? (gates P2.2 wording)
- What did "screener" mean? (gates Phase 3)
- Do Upstox terms restrict how long market data may be kept? (gates P2.6)
- Do ready-made strategies or order placement for others make this an algo provider? (gates Phase 5 scope)

---

## 6. What I verified and what I did not

**Verified here:** the 357-test suite, `tsc`, the Vite build, `npm audit`, the file and line references above, that no order-placing code exists, that `.env` was never committed, and that Upstox's Expired Instruments APIs exist on the Plus plan.

**Not verified:** Upstox plan price and expired-data history range; Upstox data-retention terms; the SEBI framework details (the source plan itself cites secondary sources); whether Upstox candles are split-adjusted; freeze limits and tag rules; Python dependency vulnerabilities.

This plan is engineering guidance, not investment or legal advice.

### Sources

- [Upstox: Expired Instruments](https://upstox.com/developer/api-documentation/expired-instruments/)
- [Upstox: Expired Instruments API launch](https://upstox.com/developer/api-documentation/announcements/expired-instruments-api/)
- [Upstox: Expired Historical Candle Data API](https://upstox.com/developer/api-documentation/get-expired-historical-candle-data/)
