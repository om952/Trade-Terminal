// Portfolio strategies: rank a universe on daily bars, hold the top N, rebalance
// on a schedule, and go to cash when a market filter fails. The editor reuses
// the builder's indicator and condition panels; the server runs the same engine
// paper trading uses (backend/portfolio).

import { useEffect, useMemo, useState } from "react";
import { api, ApiError, TERMINAL_JOB } from "./api";
import type { Deflated, IndicatorMeta, Job } from "./api";
import { ConditionPanel, IndicatorPanel, PRICE_FIELDS } from "./Builder";
import { LuckPanel } from "./Results";

type Doc = Record<string, any>;

export const PORTFOLIO_STARTERS: { key: string; label: string; spec: Doc }[] = [
  {
    key: "momentum",
    label: "Momentum rotation",
    spec: {
      name: "Momentum rotation", universe: { preset: "fno_stocks" },
      indicators: [
        { id: "mom", type: "ROC", params: { period: 126, source: "close" } },
        { id: "sma200", type: "SMA", params: { period: 200, source: "close" } },
      ],
      filter: { op: "AND", conditions: [{ left: "close", cmp: ">", right: "sma200" }] },
      rank_by: "mom", rank_order: "desc", hold: 10, rebalance: "monthly", cash_buffer: 0.02,
      regime: {
        symbol: "NIFTY",
        indicators: [{ id: "sma200", type: "SMA", params: { period: 200, source: "close" } }],
        condition: { op: "AND", conditions: [{ left: "close", cmp: ">", right: "sma200" }] },
      },
    },
  },
  {
    key: "lowvol",
    label: "Low volatility",
    spec: {
      name: "Low volatility", universe: { preset: "fno_stocks" },
      indicators: [{ id: "vol", type: "HISTVOL", params: { period: 60 } }],
      filter: null, rank_by: "vol", rank_order: "asc", hold: 15, rebalance: "monthly", cash_buffer: 0.02, regime: null,
    },
  },
];

export const BLANK_PORTFOLIO: Doc = structuredClone(PORTFOLIO_STARTERS[0].spec);

export interface PortfolioRunSettings {
  starting_capital: number;
  slippage_bps: number;
}

function references(indicators: Doc[] | undefined, catalogue: IndicatorMeta[]): string[] {
  const names = [...PRICE_FIELDS];
  for (const ind of indicators ?? []) {
    const meta = catalogue.find((m) => m.name === ind.type);
    if (meta && meta.outputs.length > 1) names.push(...meta.outputs.map((o) => `${ind.id}.${o}`));
    else names.push(ind.id);
  }
  return names;
}

/** Sidebar: the portfolio editor. */
export function PortfolioPanel({ spec, setSpec, catalogue, running, onRun }: {
  spec: Doc;
  setSpec: (d: Doc) => void;
  catalogue: IndicatorMeta[];
  running: boolean;
  onRun: (settings: PortfolioRunSettings) => void;
}) {
  const [check, setCheck] = useState<{ valid: boolean; universe?: number; errors?: { field: string; message: string }[] } | null>(null);
  const [symbolsText, setSymbolsText] = useState((spec.universe?.symbols ?? []).join(", "));
  const [settings, setSettings] = useState<PortfolioRunSettings>({ starting_capital: 1_000_000, slippage_bps: 5 });
  const usePreset = Boolean(spec.universe?.preset);
  const options = useMemo(() => references(spec.indicators, catalogue), [spec.indicators, catalogue]);
  const regimeOptions = useMemo(() => references(spec.regime?.indicators, catalogue), [spec.regime?.indicators, catalogue]);

  useEffect(() => {
    const timer = setTimeout(async () => {
      try { setCheck(await api.validatePortfolio(spec)); } catch { setCheck(null); }
    }, 300);
    return () => clearTimeout(timer);
  }, [spec]);

  const setUniverse = (preset: boolean, text = symbolsText) => {
    const symbols = text.split(/[\s,]+/).map((s: string) => s.trim()).filter(Boolean);
    setSpec({ ...spec, universe: preset ? { preset: "fno_stocks" } : { symbols } });
  };

  const regime = spec.regime as Doc | null | undefined;
  const setRegime = (next: Doc | null) => setSpec({ ...spec, regime: next });

  return (
    <>
      <div className="panel">
        <div className="panel-title">Start from</div>
        <select className="mono" value="" aria-label="Starter portfolio" onChange={(e) => {
          const starter = PORTFOLIO_STARTERS.find((s) => s.key === e.target.value);
          if (starter) setSpec({ ...structuredClone(starter.spec), universe: spec.universe });
        }}>
          <option value="">— choose a starter portfolio —</option>
          {PORTFOLIO_STARTERS.map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}
        </select>
        <div className="field" style={{ marginTop: 8 }}>
          <label htmlFor="pf-name">Name</label>
          <input id="pf-name" type="text" value={spec.name ?? ""} onChange={(e) => setSpec({ ...spec, name: e.target.value })} />
        </div>
      </div>

      <div className="panel">
        <div className="panel-title">
          <span>Universe</span>
          {check?.universe != null && <span className="hint">{check.universe} stocks</span>}
        </div>
        <div className="chip-row" style={{ marginBottom: 6 }}>
          <button className={`chip ${usePreset ? "active" : ""}`} onClick={() => setUniverse(true)}>F&amp;O stocks</button>
          <button className={`chip ${!usePreset ? "active" : ""}`} onClick={() => setUniverse(false)}>My list</button>
        </div>
        {!usePreset && (
          <textarea rows={3} className="mono" aria-label="Portfolio symbols" value={symbolsText}
            placeholder="RELIANCE, TCS, HDFCBANK"
            onChange={(e) => { setSymbolsText(e.target.value); setUniverse(false, e.target.value); }} />
        )}
      </div>

      <IndicatorPanel doc={spec} setDoc={setSpec} catalogue={catalogue} />
      <ConditionPanel title="Eligible when" node={spec.filter} options={options} optional
        onChange={(v) => setSpec({ ...spec, filter: v })} />

      <div className="panel">
        <div className="panel-title">Rank and hold</div>
        <div className="row">
          <div className="field">
            <label htmlFor="pf-rank">Rank by</label>
            <select id="pf-rank" className="mono" value={typeof spec.rank_by === "string" ? spec.rank_by : ""}
              onChange={(e) => setSpec({ ...spec, rank_by: e.target.value })}>
              {options.map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
          </div>
          <div className="field">
            <label htmlFor="pf-order">Keep</label>
            <select id="pf-order" className="mono" value={spec.rank_order ?? "desc"}
              onChange={(e) => setSpec({ ...spec, rank_order: e.target.value })}>
              <option value="desc">highest</option><option value="asc">lowest</option>
            </select>
          </div>
        </div>
        <div className="row">
          <div className="field">
            <label htmlFor="pf-hold">Hold</label>
            <input id="pf-hold" type="number" min={1} max={50} className="mono" value={spec.hold ?? 10}
              onChange={(e) => setSpec({ ...spec, hold: Number(e.target.value) || 1 })} />
          </div>
          <div className="field">
            <label htmlFor="pf-rebalance">Rebalance</label>
            <select id="pf-rebalance" className="mono" value={spec.rebalance ?? "monthly"}
              onChange={(e) => setSpec({ ...spec, rebalance: e.target.value })}>
              <option value="weekly">weekly</option><option value="monthly">monthly</option>
            </select>
          </div>
        </div>
        <div className="hint">Equal weight; decided at a session's close and filled at the next open.</div>
      </div>

      <div className="panel">
        <div className="panel-title">
          <span>Market filter</span>
          <label className="row" style={{ gap: 4, alignItems: "center", flex: "0 0 auto" }}>
            <input type="checkbox" style={{ width: "auto" }} aria-label="Use a market filter" checked={Boolean(regime)}
              onChange={(e) => setRegime(e.target.checked ? structuredClone(PORTFOLIO_STARTERS[0].spec.regime) : null)} />
          </label>
        </div>
        {regime ? (
          <>
            <div className="field">
              <label htmlFor="pf-benchmark">Benchmark</label>
              <input id="pf-benchmark" type="text" className="mono" value={regime.symbol ?? ""}
                onChange={(e) => setRegime({ ...regime, symbol: e.target.value })} />
            </div>
            <IndicatorPanel doc={regime} setDoc={(d) => setRegime(d)} catalogue={catalogue} />
            <ConditionPanel title="Invested while" node={regime.condition} options={regimeOptions}
              onChange={(v) => setRegime({ ...regime, condition: v ?? { op: "AND", conditions: [] } })} />
            <div className="hint">When this is false on a rebalance day, the portfolio sells everything and holds cash.</div>
          </>
        ) : <div className="hint">Off: always invested in the top stocks.</div>}
      </div>

      <div className="panel">
        <div className="row">
          <div className="field">
            <label htmlFor="pf-capital">Capital (₹)</label>
            <input id="pf-capital" type="number" className="mono" value={settings.starting_capital}
              onChange={(e) => setSettings({ ...settings, starting_capital: Number(e.target.value) || 1 })} />
          </div>
          <div className="field">
            <label htmlFor="pf-slippage">Slippage (bps)</label>
            <input id="pf-slippage" type="number" step="0.5" className="mono" value={settings.slippage_bps}
              onChange={(e) => setSettings({ ...settings, slippage_bps: Number(e.target.value) })} />
          </div>
        </div>
      </div>

      {check && !check.valid && (
        <div className="notice error">
          <div className="notice-title">Cannot run — fix these first</div>
          <ul>{check.errors?.map((e, i) => <li key={i}>{e.field && <code>{e.field}</code>} {e.message}</li>)}</ul>
        </div>
      )}
      <button className="btn" style={{ width: "100%" }} disabled={!check?.valid || running} onClick={() => onRun(settings)}>
        {running ? <><span className="spinner" /> Running…</> : "Backtest portfolio"}
      </button>
    </>
  );
}

/* ------------------------------------------------------------------ */

export interface PortfolioRun {
  name: string;
  synthetic_data: boolean;
  data: { instruments: number; benchmark: string | null; first_bar: string; last_bar: string;
    missing: { symbol: string; reason: string }[] };
  metrics: Record<string, number | null>;
  trades: { symbol: string; entry_time: string; exit_time: string; quantity: number; entry_price: number;
    exit_price: number; net_pnl: number; reason: string }[];
  equity_curve: { time: number; equity: number; drawdown: number }[];
  rebalances: { date: string; regime_on: boolean; eligible: number; targets: string[]; orders: number }[];
  holdings: { symbol: string; quantity: number; avg_price: number; last_close: number; since: string }[];
  notes: string[];
  deflated?: Deflated;
}

function fmt(n: number | null | undefined, digits = 2) {
  return n == null ? "–" : n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function EquityLine({ points }: { points: { equity: number }[] }) {
  if (points.length < 2) return null;
  const values = points.map((p) => p.equity);
  const lo = Math.min(...values), hi = Math.max(...values), span = hi - lo || 1;
  const w = 800, h = 160;
  const path = values.map((v, i) => `${(i / (values.length - 1)) * w},${h - ((v - lo) / span) * h}`).join(" ");
  return (
    <svg viewBox={`0 0 ${w} ${h}`} width="100%" height="160" role="img" aria-label="Equity curve" preserveAspectRatio="none">
      <polyline fill="none" stroke="var(--accent)" strokeWidth="1.5" points={path} vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

/** Main area: a portfolio run's results. */
export function PortfolioView({ job, run }: { job: Job<{ run_id: string }> | null; run: PortfolioRun | null }) {
  const running = job != null && !TERMINAL_JOB.includes(job.status);
  const m = run?.metrics ?? {};
  return (
    <div>
      {running && (
        <div className="panel" role="status">
          <span><span className="spinner" /> {job.message || "Starting…"}</span>
          <div className="progress"><div style={{ width: `${Math.round(job.progress * 100)}%` }} /></div>
        </div>
      )}
      {job?.status === "failed" && <div className="notice error"><div className="notice-title">Portfolio backtest failed</div>{job.error}</div>}

      {run && (
        <>
          <div className="row" style={{ justifyContent: "space-between", alignItems: "baseline", marginBottom: 8 }}>
            <b style={{ flex: "1 1 auto" }}>{run.name}</b>
            <span className="hint" style={{ flex: "0 0 auto" }}>
              {run.data.instruments} stocks · {run.data.first_bar.slice(0, 10)} to {run.data.last_bar.slice(0, 10)}
              {run.data.benchmark ? ` · filter on ${run.data.benchmark}` : ""}
            </span>
          </div>
          {run.notes.length > 0 && <ul className="caveats">{run.notes.map((n, i) => <li key={i}>{n}</li>)}</ul>}

          <div className="metric-grid" aria-label="Portfolio results">
            {([["Net P&L", m.net_pnl, 0], ["Realized", m.realized_pnl, 0], ["Open", m.unrealized_pnl, 0],
              ["Return", m.total_return_pct, 2, "%"], ["CAGR", m.cagr_pct, 2, "%"],
              ["Max drawdown", m.max_drawdown_pct, 2, "%"], ["Sharpe", m.sharpe, 2], ["Trades", m.total_trades, 0]] as
              [string, number | null, number, string?][]).map(([label, value, digits, unit]) => (
              <div className="metric" key={label}>
                <div className="metric-label">{label}</div>
                <div className={`metric-value ${label.startsWith("Net") || label === "Return" ? ((value ?? 0) > 0 ? "pos" : (value ?? 0) < 0 ? "neg" : "") : ""}`}>
                  {fmt(value, digits)}{unit ?? ""}
                </div>
              </div>
            ))}
          </div>

          {run.deflated && <LuckPanel d={run.deflated} />}

          <h3 className="section-title">Equity</h3>
          <EquityLine points={run.equity_curve} />

          <h3 className="section-title">Holding now ({run.holdings.length})</h3>
          {run.holdings.length === 0 ? <p className="hint">In cash.</p> : (
            <div className="table-wrap">
              <table style={{ minWidth: 0 }} aria-label="Holdings">
                <thead><tr><th>Stock</th><th>Quantity</th><th>Average price</th><th>Last close</th><th>Since</th></tr></thead>
                <tbody>
                  {run.holdings.map((h) => (
                    <tr key={h.symbol}><td><b>{h.symbol}</b></td><td className="mono">{h.quantity}</td>
                      <td className="mono">{fmt(h.avg_price)}</td><td className="mono">{fmt(h.last_close)}</td><td className="mono">{h.since}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <h3 className="section-title">Rebalances ({run.rebalances.length})</h3>
          <div className="table-wrap table-scroll">
            <table style={{ minWidth: 0 }} aria-label="Rebalances">
              <thead><tr><th>Date</th><th>Market filter</th><th>Eligible</th><th>Held after</th><th>Orders</th></tr></thead>
              <tbody>
                {[...run.rebalances].reverse().map((r) => (
                  <tr key={r.date}>
                    <td className="mono">{r.date}</td>
                    <td className={r.regime_on ? "pos" : "neg"}>{r.regime_on ? "invested" : "cash"}</td>
                    <td className="mono">{r.eligible}</td>
                    <td className="mono">{r.targets.join(", ") || "–"}</td>
                    <td className="mono">{r.orders}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <h3 className="section-title">Closed trades ({run.trades.length})</h3>
          {run.trades.length > 0 && (
            <div className="table-wrap table-scroll">
              <table style={{ minWidth: 0 }} aria-label="Portfolio trades">
                <thead><tr><th>Stock</th><th>Entry</th><th>Exit</th><th>Quantity</th><th>Net P&amp;L</th><th>Why</th></tr></thead>
                <tbody>
                  {[...run.trades].reverse().map((t, i) => (
                    <tr key={i}>
                      <td>{t.symbol}</td><td className="mono">{String(t.entry_time).slice(0, 10)}</td>
                      <td className="mono">{String(t.exit_time).slice(0, 10)}</td><td className="mono">{t.quantity}</td>
                      <td className={`mono ${t.net_pnl > 0 ? "pos" : t.net_pnl < 0 ? "neg" : ""}`}>{fmt(t.net_pnl, 0)}</td>
                      <td className="hint">{t.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {run.data.missing.length > 0 && (
            <details style={{ marginTop: 8 }}>
              <summary className="hint">{run.data.missing.length} not loaded</summary>
              <ul className="caveats">{run.data.missing.map((x) => <li key={x.symbol}><b>{x.symbol}</b>: {x.reason}</li>)}</ul>
            </details>
          )}
        </>
      )}
    </div>
  );
}

export function usePortfolioRun(job: Job<{ run_id: string }> | null, onError: (m: string) => void) {
  const [run, setRun] = useState<PortfolioRun | null>(null);
  useEffect(() => {
    if (job?.status !== "succeeded" || !job.result) return;
    // The run is recorded before its deflated Sharpe is computed (the trial count
    // includes it), so the deflated figure comes from the job's result.
    const deflated = (job.result as { deflated?: Deflated }).deflated;
    api.run(job.result.run_id).then((r) => setRun({ ...(r.result as unknown as PortfolioRun), deflated }))
      .catch((e) => onError((e as ApiError).message));
  }, [job?.status, job?.result?.run_id]);
  return [run, setRun] as const;
}
