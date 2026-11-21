import { useCallback, useEffect, useState } from "react";
import { api, ApiError, TERMINAL_JOB } from "./api";
import type {
  BacktestResult, Dataset, IndicatorMeta, Job, RunSummary, ScanResult, Status, SweepResult, TemplateMeta,
  ValidationResult,
} from "./api";
import { Assistant } from "./Assistant";
import { Builder, type RunConfig } from "./Builder";
import { Library } from "./Library";
import { ChartTab, PerformanceTab, TradeLogTab } from "./Results";
import { BLANK_LIVE_CONFIG, LiveDashboard, LivePanel, type LiveConfig } from "./Live";
import { SweepTable, sweepCount } from "./Sweep";
import { BLANK_SCAN, ScanResults, ScreenerPanel, useJobPoll } from "./Screener";
import { KillSwitchBanner, KillSwitchButton, useKillSwitch } from "./KillSwitch";
import { ValidationView, type RobustnessResult, type ValidateOptions } from "./Validation";
import { ResearchPanel, ResearchView, openSaved } from "./Research";
import { BLANK_PORTFOLIO, PortfolioPanel, PortfolioView, usePortfolioRun, type PortfolioRun, type PortfolioRunSettings } from "./Portfolio";
import { PaperPortfolios } from "./PaperPortfolio";
import type { PortfolioStep, ResearchSession } from "./api";

const BLANK_STRATEGY = {
  name: "New Strategy",
  instrument: { symbol: "", segment: "index" },
  timeframe: "5m",
  session: { start: "09:20", end: "15:10", square_off: "15:15" },
  indicators: [
    { id: "ema_fast", type: "EMA", params: { period: 9, source: "close" } },
    { id: "ema_slow", type: "EMA", params: { period: 20, source: "close" } },
  ],
  entry: { op: "AND", conditions: [{ left: "ema_fast", cmp: "crosses_above", right: "ema_slow" }] },
  exit: { op: "OR", conditions: [{ left: "ema_fast", cmp: "crosses_below", right: "ema_slow" }] },
  legs: [{ id: "leg1", action: "BUY", kind: "equity", lots: 1 }],
  risk: { max_concurrent_positions: 1 },
};

type Tab = "chart" | "performance" | "trades" | "sweep" | "scan" | "portfolio" | "validation" | "research" | "live";

const SWEEP_POLL_MS = 1000;
type Panel = "assistant" | "builder" | "screener" | "research" | "library";

export default function App() {
  const [theme, setTheme] = useState<"dark" | "light">("dark");
  const [status, setStatus] = useState<Status | null>(null);
  const [indicators, setIndicators] = useState<IndicatorMeta[]>([]);
  const [templates, setTemplates] = useState<TemplateMeta[]>([]);
  const [datasets, setDatasets] = useState<Dataset[]>([]);

  const [doc, setDoc] = useState<Record<string, any>>(BLANK_STRATEGY);
  const [validation, setValidation] = useState<ValidationResult | null>(null);
  const [runConfig, setRunConfig] = useState<RunConfig>({
    symbol: "",
    starting_capital: 1_000_000,
    lot_size: 1,
    slippage_bps: 5,
    max_leverage: 5,
  });

  const [liveConfig, setLiveConfig] = useState<LiveConfig>(BLANK_LIVE_CONFIG);
  const [sessionId, setSessionId] = useState<string | null>(null);

  const [result, setResult] = useState<BacktestResult | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The account kill switch, in the header so it is one click from anywhere.
  const kill = useKillSwitch(setError);
  const [tab, setTab] = useState<Tab>("performance");
  const [panel, setPanel] = useState<Panel>("assistant");
  const [strategyId, setStrategyId] = useState<string | null>(null);
  const [saveNote, setSaveNote] = useState<string | null>(null);
  const [interrupted, setInterrupted] = useState(0);
  const [sweepJob, setSweepJob] = useState<Job<SweepResult> | null>(null);
  const [sweep, setSweep] = useState<SweepResult | null>(null);
  const sweeping = sweepJob != null && !TERMINAL_JOB.includes(sweepJob.status);
  const [scanSpec, setScanSpec] = useState<Record<string, any>>(BLANK_SCAN);
  const [scanJob, setScanJob] = useState<Job<ScanResult> | null>(null);
  const [savedScanId, setSavedScanId] = useState<string | null>(null);
  const scanning = scanJob != null && !TERMINAL_JOB.includes(scanJob.status);
  useJobPoll(scanJob, setScanJob, setError);

  // The final test: this idea's one run on the held-out data (backend/services/holdout.py).
  const [finalJob, setFinalJob] = useState<Job<{ run_id: string }> | null>(null);
  const finalTesting = finalJob != null && !TERMINAL_JOB.includes(finalJob.status);
  useJobPoll(finalJob, setFinalJob, setError);
  useEffect(() => {
    if (finalJob?.status === "succeeded" && finalJob.result) {
      api.run(finalJob.result.run_id).then((run) => { setResult(run.result); setTab("performance"); })
        .catch((e) => setError((e as ApiError).message));
    }
    if (finalJob?.status === "failed") setError(`Final test failed: ${finalJob.error}`);
  }, [finalJob?.status]);

  const runFinalTest = async () => {
    const ok = window.confirm(
      "Run the final test on the held-out data?\n\nEach idea gets one. Changing numbers afterwards " +
      "keeps it the same idea, so it cannot be tested on the held-out data again.",
    );
    if (!ok) return;
    setError(null);
    try {
      setFinalJob(await api.finalTest({
        strategy: doc,
        symbol: runConfig.symbol || undefined,
        starting_capital: runConfig.starting_capital,
        lot_size: runConfig.lot_size,
        slippage_bps: runConfig.slippage_bps,
        max_leverage: runConfig.max_leverage,
      }));
    } catch (e) {
      const err = e as ApiError;
      const detail = err.detail as { message?: string; run_id?: string } | undefined;
      setError(detail?.message ? `${detail.message} (run ${detail.run_id ?? "still running"})` : err.message);
    }
  };

  // Robustness checks of the strategy in the editor (backend/services/validate.py).
  const [validateJob, setValidateJob] = useState<Job<RobustnessResult> | null>(null);
  const validating = validateJob != null && !TERMINAL_JOB.includes(validateJob.status);
  useJobPoll(validateJob, setValidateJob, setError);

  const runValidate = async (options: ValidateOptions) => {
    setError(null);
    try {
      setValidateJob(await api.checkRobustness({
        strategy: doc,
        symbol: runConfig.symbol || undefined,
        starting_capital: runConfig.starting_capital,
        lot_size: runConfig.lot_size,
        slippage_bps: runConfig.slippage_bps,
        max_leverage: runConfig.max_leverage,
        ...options,
      }));
      setTab("validation");
    } catch (e) {
      setError((e as ApiError).message || "Checks did not start");
    }
  };

  // Research runs (backend/research). Polled while a turn is working.
  const [research, setResearch] = useState<ResearchSession | null>(null);
  useEffect(() => {
    if (!research || research.phase !== "working") return;
    const timer = setTimeout(async () => {
      try { setResearch(await api.research(research.id)); } catch (e) { setError((e as ApiError).message); }
    }, 1000);
    return () => clearTimeout(timer);
  }, [research]);

  const showResearch = (s: ResearchSession | null) => {
    setResearch(s);
    if (s) setTab("research");
  };

  // Portfolio strategies (backend/portfolio), edited in the Screener's Portfolio mode.
  const [screenerMode, setScreenerMode] = useState<"scan" | "portfolio">("scan");
  const [portfolioSpec, setPortfolioSpec] = useState<Record<string, any>>(BLANK_PORTFOLIO);
  const [portfolioJob, setPortfolioJob] = useState<Job<{ run_id: string }> | null>(null);
  const portfolioRunning = portfolioJob != null && !TERMINAL_JOB.includes(portfolioJob.status);
  useJobPoll(portfolioJob, setPortfolioJob, setError);
  const [portfolioRun, setPortfolioRun] = usePortfolioRun(portfolioJob, setError);

  // Paper portfolio sessions (backend/portfolio/session.py), shown in the Portfolio tab.
  const [hasPaper, setHasPaper] = useState(false);
  const [paperFocus, setPaperFocus] = useState<string | null>(null);
  const [paperVersion, setPaperVersion] = useState(0);
  const [paperJob, setPaperJob] = useState<Job<PortfolioStep> | null>(null);
  useEffect(() => {
    api.portfolioSessions().then((r) => setHasPaper(r.sessions.length > 0)).catch(() => undefined);
  }, []);
  const startPaperPortfolio = async (run: PortfolioRun) => {
    setError(null);
    try {
      const made = await api.startPortfolioSession({
        spec: run.document, starting_capital: Number(run.params?.starting_capital ?? 1_000_000),
        slippage_bps: Number(run.params?.slippage_bps ?? 5),
      });
      setHasPaper(true);
      setPaperFocus(made.id);
      setPaperJob(made.job);
      setPaperVersion((v) => v + 1);
    } catch (e) { setError((e as ApiError).message); }
  };

  const runPortfolio = async (settings: PortfolioRunSettings) => {
    setError(null);
    setPortfolioRun(null);
    try {
      setPortfolioJob(await api.portfolio({ spec: portfolioSpec, ...settings }));
      setTab("portfolio");
    } catch (e) {
      setError((e as ApiError).message || "Portfolio backtest did not start");
    }
  };

  const runScan = async (mode: "now" | "history", override?: Record<string, any>) => {
    setError(null);
    try {
      setScanJob(await api.scan({ spec: override ?? scanSpec, mode }));
      setTab("scan");
    } catch (e) {
      setError((e as ApiError).message || "Scan did not start");
    }
  };

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
  }, [theme]);

  const loadDatasets = useCallback(async () => {
    try { setDatasets((await api.datasets()).datasets); } catch { /* backend down */ }
  }, []);

  useEffect(() => {
    (async () => {
      try {
        const [s, ind, tpl] = await Promise.all([api.status(), api.indicators(), api.templates()]);
        setStatus(s);
        setIndicators(ind.indicators);
        setTemplates(tpl.templates);
        await loadDatasets();
        // Sessions cut off by a crash are listed, never resumed silently.
        const sessions = await api.liveSessions();
        setInterrupted(sessions.history.filter((h) => h.state === "interrupted").length);
      } catch (e) {
        setError(
          "Cannot reach the backend. Start it with:  .venv/bin/uvicorn backend.api.main:app --port 8000",
        );
      }
    })();
  }, [loadDatasets]);

  // Validate on every edit, debounced. Surfacing a bad indicator reference
  // immediately beats discovering it as an empty trade log after a long run.
  useEffect(() => {
    const timer = setTimeout(async () => {
      try { setValidation(await api.validate(doc)); } catch { /* backend down */ }
    }, 300);
    return () => clearTimeout(timer);
  }, [doc]);

  // Keep the run symbol and the document's instrument in step; the backtest
  // endpoint accepts an override, and a silent mismatch would be confusing.
  useEffect(() => {
    if (doc.instrument?.symbol && !runConfig.symbol) {
      setRunConfig((c) => ({ ...c, symbol: doc.instrument.symbol }));
    }
  }, [doc.instrument?.symbol]);

  // The paper session trades whatever the backtest just tested. Letting these
  // drift apart is how you end up paper trading a different instrument than the
  // one whose results convinced you to.
  useEffect(() => {
    const symbol = runConfig.symbol || doc.instrument?.symbol || "";
    setLiveConfig((c) => ({
      ...c,
      symbol,
      starting_capital: runConfig.starting_capital,
      lot_size: runConfig.lot_size,
      slippage_bps: runConfig.slippage_bps,
      max_leverage: runConfig.max_leverage,
    }));
  }, [runConfig, doc.instrument?.symbol]);

  // Poll a running sweep. The grid runs server-side as a job, so closing the
  // tab does not stop it; the recorded runs stay in the library either way.
  useEffect(() => {
    if (!sweepJob || TERMINAL_JOB.includes(sweepJob.status)) return;
    const timer = setTimeout(async () => {
      try {
        const next = await api.job<SweepResult>(sweepJob.id);
        setSweepJob(next);
        if (next.status === "succeeded" && next.result) setSweep(next.result);
        if (next.status === "failed") setError(`Sweep failed: ${next.error}`);
      } catch (e) {
        setError((e as ApiError).message);
      }
    }, SWEEP_POLL_MS);
    return () => clearTimeout(timer);
  }, [sweepJob]);

  const runSweep = async () => {
    setError(null);
    setSweep(null);
    try {
      setSweepJob(await api.sweep({
        strategy: doc,
        strategy_id: strategyId ?? undefined,
        symbol: runConfig.symbol || undefined,
        starting_capital: runConfig.starting_capital,
        lot_size: runConfig.lot_size,
        slippage_bps: runConfig.slippage_bps,
        max_leverage: runConfig.max_leverage,
      }));
      setTab("sweep");
    } catch (e) {
      setError((e as ApiError).message || "Sweep did not start");
    }
  };

  const cancelSweep = async () => {
    if (!sweepJob) return;
    try { setSweepJob(await api.cancelJob(sweepJob.id) as Job<SweepResult>); } catch { /* finished meanwhile */ }
  };

  const run = async () => {
    if (sweepCount(doc) > 0) return runSweep();
    setRunning(true);
    setError(null);
    try {
      const payload = await api.backtest({
        strategy: doc,
        symbol: runConfig.symbol || undefined,
        starting_capital: runConfig.starting_capital,
        lot_size: runConfig.lot_size,
        slippage_bps: runConfig.slippage_bps,
        max_leverage: runConfig.max_leverage,
        include_chart: true,
      });
      setResult(payload);
      setTab("performance");
    } catch (e) {
      const err = e as ApiError;
      setError(err.message || "Backtest failed");
      setResult(null);
    } finally {
      setRunning(false);
    }
  };

  // Opening a run shows its report. A sweep variant's report keeps the swept
  // document in the builder unless the trader chooses to use that variant.
  const openRun = async (runId: string, opts: { keepDocument?: boolean } = {}) => {
    setError(null);
    try {
      const run = await api.run(runId);
      setResult(run.result);
      if (!opts.keepDocument) {
        setDoc(run.document);
        setStrategyId(run.strategy_id);
      }
      setTab("performance");
    } catch (e) {
      setError((e as ApiError).message);
    }
  };

  const openDocument = (document: Record<string, unknown>, id: string | null) => {
    setDoc(document);
    setStrategyId(id);
    setPanel("builder");
  };

  // One click from a backtest to a paper session on the same strategy and
  // settings. Synthetic data cannot be traded live, so it replays instead: the
  // last fifth of the backtest window, so the earlier bars warm the indicators
  // up exactly as the backtest's did. Replaying from the first bar would start
  // cold and diverge from the backtest.
  const startPaper = async (document: Record<string, unknown>, run: RunSummary) => {
    setError(null);
    const synthetic = run.symbol.startsWith("SIM|");
    const first = Date.parse(run.data.first_bar ?? "");
    const last = Date.parse(run.data.last_bar ?? "");
    const replayStart = Number.isFinite(first) && Number.isFinite(last)
      ? new Date(first + (last - first) * 0.8).toISOString().slice(0, 10)
      : undefined;
    try {
      const started = await api.liveStart({
        strategy: document,
        symbol: run.symbol,
        starting_capital: run.metrics.starting_capital,
        lot_size: 1,
        source: synthetic ? "replay" : "live",
        ...(synthetic ? { replay_start: replayStart, replay_speed: 60 } : {}),
        alerts: true,
      });
      setSessionId(started.session_id);
      setTab("live");
    } catch (e) {
      setError(`Paper trading did not start: ${(e as ApiError).message}`);
    }
  };

  const saveToLibrary = async () => {
    setSaveNote(null);
    try {
      const saved = await api.saveStrategy(doc, strategyId ?? undefined);
      setStrategyId(saved.strategy_id);
      setDoc(saved.document ?? doc);
      setSaveNote(saved.new_version ? `Saved as version ${saved.version}` : `No changes since version ${saved.version}`);
    } catch (e) {
      setSaveNote(`Not saved: ${(e as ApiError).message}`);
    }
  };

  return (
    <div className="app">
      <header className="header">
        <span className="brand">Trade Terminal</span>

        <div className="status-row">
          {status && (
            <>
              <span className="status-chip">
                <span className={`dot ${status.auth.valid ? "ok" : "bad"}`} />
                Upstox {status.auth.valid ? <b>{status.auth.hours_remaining}h left</b> : <b>expired</b>}
              </span>
              <span className="status-chip">
                <span className="dot ok" /> <b>{status.instruments.toLocaleString()}</b> instruments
              </span>
              <span className="status-chip">
                <span className={`dot ${status.datasets ? "ok" : "warn"}`} />
                <b>{status.datasets}</b> datasets
              </span>
              <span className="status-chip">
                <span className={`dot ${status.risk_configured ? "ok" : "warn"}`} />
                mode <b>{status.mode}</b>
              </span>
            </>
          )}
          <KillSwitchButton kill={kill} />
          <button className="icon-btn" onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>
            {theme === "dark" ? "Light" : "Dark"}
          </button>
        </div>
      </header>
      <KillSwitchBanner kill={kill} />

      <div className="main">
        <aside className="sidebar">
          <div className="seg-control" role="tablist">
            {(["assistant", "builder", "screener", "research", "library"] as Panel[]).map((p) => (
              <button key={p} role="tab" aria-selected={panel === p}
                className={panel === p ? "active" : ""} onClick={() => setPanel(p)}>
                {p === "assistant" ? "Assistant" : p === "builder" ? "Builder" : p === "screener" ? "Screener"
                  : p === "research" ? "Research" : "Library"}
              </button>
            ))}
          </div>

          {panel === "assistant" && (
            <Assistant onOpenRun={openRun} onStartPaper={startPaper} onOpenDocument={openDocument} />
          )}

          {panel === "library" && <Library onOpenDocument={openDocument} onOpenRun={openRun} />}

          {panel === "research" && (
            <ResearchPanel doc={doc} sessionId={research?.id ?? null} onSession={showResearch}
              settings={{ symbol: runConfig.symbol, starting_capital: runConfig.starting_capital,
                lot_size: runConfig.lot_size, slippage_bps: runConfig.slippage_bps }} />
          )}

          {panel === "screener" && (
            <>
              <div className="chip-row" style={{ marginBottom: 8 }} role="tablist" aria-label="Screener mode">
                {(["scan", "portfolio"] as const).map((m) => (
                  <button key={m} role="tab" aria-selected={screenerMode === m}
                    className={`chip ${screenerMode === m ? "active" : ""}`} onClick={() => setScreenerMode(m)}>
                    {m === "scan" ? "Scan" : "Portfolio"}
                  </button>
                ))}
              </div>
              {screenerMode === "scan" ? (
                <ScreenerPanel spec={scanSpec} setSpec={setScanSpec} catalogue={indicators}
                  running={scanning} onRun={runScan} savedId={savedScanId} setSavedId={setSavedScanId} />
              ) : (
                <PortfolioPanel spec={portfolioSpec} setSpec={setPortfolioSpec} catalogue={indicators}
                  running={portfolioRunning} onRun={runPortfolio} />
              )}
            </>
          )}

          {panel === "builder" && <>
          <div className="row" style={{ gap: 6, marginBottom: 8 }}>
            <button className="btn secondary tiny" disabled={!validation?.valid} onClick={saveToLibrary}>
              {strategyId ? "Save new version" : "Save to library"}
            </button>
            {saveNote && <span className="hint">{saveNote}</span>}
          </div>
          <Builder
            doc={doc}
            setDoc={setDoc}
            indicators={indicators}
            templates={templates}
            datasets={datasets}
            validation={validation}
            onRun={run}
            running={running || sweeping}
            runningLabel={sweeping ? sweepJob?.message || "Starting the sweep…" : undefined}
            runConfig={runConfig}
            setRunConfig={setRunConfig}
            onRefreshDatasets={loadDatasets}
          />

          <LivePanel
            doc={doc}
            datasets={datasets}
            config={liveConfig}
            setConfig={setLiveConfig}
            valid={validation?.valid ?? false}
            onStarted={(id) => {
              setSessionId(id);
              setTab("live");
            }}
          />
          </>}
        </aside>

        <main className="content">
          <nav className="tabs">
            {(["chart", "performance", "trades", "sweep", "scan", "portfolio", "validation", "research", "live"] as Tab[])
              .filter((t) => (t !== "sweep" || sweepJob || sweep) && (t !== "scan" || scanJob) && (t !== "portfolio" || portfolioJob || hasPaper)
                && (t !== "validation" || validateJob) && (t !== "research" || research))
              .map((t) => (
              <button
                key={t}
                className={`tab ${tab === t ? "active" : ""}`}
                onClick={() => setTab(t)}
                disabled={!["live", "sweep", "scan", "portfolio", "validation", "research"].includes(t) && (!result || (t === "chart" && !result.chart))}
              >
                {t === "chart"
                  ? "Chart"
                  : t === "performance"
                    ? "Performance"
                    : t === "trades"
                      ? `Trade Log${result ? ` (${result.trades.length})` : ""}`
                      : t === "sweep"
                        ? `Sweep${sweep ? ` (${sweep.variants.length})` : ""}`
                        : t === "scan"
                          ? "Scan"
                          : t === "portfolio"
                            ? "Portfolio"
                          : t === "validation"
                            ? "Robustness"
                            : t === "research"
                              ? "Research"
                              : "Live"}
              </button>
            ))}
            {result && (
              <span className="status-chip" style={{ marginLeft: "auto", paddingRight: 4 }}>
                {result.symbol} · {result.timeframe} · {result.strategy_hash.slice(0, 8)}
              </span>
            )}
          </nav>

          <div className="tab-body">
            {interrupted > 0 && (
              <div className="notice warn">
                <div className="notice-title">
                  {interrupted} paper session{interrupted > 1 ? "s were" : " was"} interrupted
                </div>
                The app stopped while {interrupted > 1 ? "they were" : "it was"} running. Nothing was
                resumed automatically.{" "}
                <button className="btn secondary tiny" onClick={() => { setTab("live"); setInterrupted(0); }}>
                  Review in Live
                </button>{" "}
                <button className="btn secondary tiny" onClick={() => setInterrupted(0)}>Dismiss</button>
              </div>
            )}

            {error && (
              <div className="notice error">
                <div className="notice-title">Error</div>
                {error}
              </div>
            )}

            {tab === "live" && <LiveDashboard sessionId={sessionId} onPick={setSessionId} />}

            {tab === "sweep" && sweepJob && sweeping && (
              <div className="panel" role="status">
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <span><span className="spinner" /> {sweepJob.message || "Starting the sweep…"}</span>
                  <button className="btn secondary tiny" onClick={cancelSweep}>Cancel</button>
                </div>
                <div className="progress"><div style={{ width: `${Math.round(sweepJob.progress * 100)}%` }} /></div>
              </div>
            )}
            {tab === "sweep" && sweepJob?.status === "cancelled" && (
              <div className="notice warn">
                Sweep cancelled. The variants that finished are recorded and listed in the library.
              </div>
            )}
            {tab === "sweep" && sweep && (
              <SweepTable
                sweep={sweep}
                onOpenRun={(id) => openRun(id, { keepDocument: true })}
                onUseRun={(id) => { openRun(id); setPanel("builder"); }}
              />
            )}

            {tab === "scan" && (
              <ScanResults job={scanJob} result={scanJob?.status === "succeeded" ? scanJob.result : null}
                onCancel={async () => {
                  if (!scanJob) return;
                  try { setScanJob(await api.cancelJob(scanJob.id) as Job<ScanResult>); } catch { /* finished */ }
                }} />
            )}

            {tab === "validation" && validateJob && (
              <ValidationView job={validateJob} onOpenRun={(id) => openRun(id, { keepDocument: true })}
                onCancel={async () => {
                  try { setValidateJob(await api.cancelJob(validateJob.id) as Job<RobustnessResult>); } catch { /* finished */ }
                }} />
            )}

            {tab === "research" && research && (
              <ResearchView session={research}
                onReply={async (payload) => {
                  try { setResearch(await api.researchReply(research.id, payload)); } catch (e) { setError((e as ApiError).message); }
                }}
                onOpenRun={(id) => openRun(id, { keepDocument: true })}
                onOpenStrategy={async (id) => {
                  try { openDocument(await openSaved(id), id); } catch (e) { setError((e as ApiError).message); }
                }} />
            )}

            {tab === "portfolio" && (
              <>
                <PortfolioView job={portfolioJob} run={portfolioRun} onPaper={startPaperPortfolio} />
                <PaperPortfolios focus={paperFocus} setFocus={setPaperFocus} version={paperVersion}
                  startJob={paperJob} onError={setError} />
              </>
            )}

            {!["live", "sweep", "scan", "portfolio", "validation", "research"].includes(tab) && !result && !error && (
              <div className="empty">
                <b>No backtest yet</b>
                <span>Describe a strategy to the assistant, or build one by hand, then run it.</span>
                <span>Market data is downloaded automatically the first time it is needed.</span>
              </div>
            )}

            {result && tab === "chart" && <ChartTab result={result} />}
            {result && tab === "performance" && (
              <PerformanceTab result={result} onFinalTest={runFinalTest} finalTesting={finalTesting}
                onValidate={runValidate} validating={validating} />
            )}
            {result && tab === "trades" && <TradeLogTab result={result} />}
          </div>
        </main>
      </div>
    </div>
  );
}
