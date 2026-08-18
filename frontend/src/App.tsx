import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "./api";
import type { BacktestResult, Dataset, IndicatorMeta, Status, TemplateMeta, ValidationResult } from "./api";
import { Builder, type RunConfig } from "./Builder";
import { ChartTab, PerformanceTab, TradeLogTab } from "./Results";
import { BLANK_LIVE_CONFIG, LiveDashboard, LivePanel, type LiveConfig } from "./Live";

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

type Tab = "chart" | "performance" | "trades" | "live";

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
  const [tab, setTab] = useState<Tab>("performance");

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

  const run = async () => {
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
          <button className="icon-btn" onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>
            {theme === "dark" ? "Light" : "Dark"}
          </button>
        </div>
      </header>

      <div className="main">
        <aside className="sidebar">
          <Builder
            doc={doc}
            setDoc={setDoc}
            indicators={indicators}
            templates={templates}
            datasets={datasets}
            validation={validation}
            onRun={run}
            running={running}
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
        </aside>

        <main className="content">
          <nav className="tabs">
            {(["chart", "performance", "trades", "live"] as Tab[]).map((t) => (
              <button
                key={t}
                className={`tab ${tab === t ? "active" : ""}`}
                onClick={() => setTab(t)}
                disabled={t !== "live" && !result}
              >
                {t === "chart"
                  ? "Chart"
                  : t === "performance"
                    ? "Performance"
                    : t === "trades"
                      ? `Trade Log${result ? ` (${result.trades.length})` : ""}`
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
            {error && (
              <div className="notice error">
                <div className="notice-title">Error</div>
                {error}
              </div>
            )}

            {tab === "live" && <LiveDashboard sessionId={sessionId} onPick={setSessionId} />}

            {tab !== "live" && !result && !error && (
              <div className="empty">
                <b>No backtest yet</b>
                <span>Pick a template or build a strategy, choose a dataset, then run.</span>
                {datasets.length === 0 && (
                  <span>No data stored yet — search an instrument and download its history first.</span>
                )}
              </div>
            )}

            {result && tab === "chart" && <ChartTab result={result} />}
            {result && tab === "performance" && <PerformanceTab result={result} />}
            {result && tab === "trades" && <TradeLogTab result={result} />}
          </div>
        </main>
      </div>
    </div>
  );
}
