// Robustness checks of one strategy: walk-forward, neighbourhood (swept
// strategies), slippage stress and a second instrument. Every figure comes
// from a recorded run the trader can open; the verdicts are computed on the
// server from those runs.

import { useState } from "react";
import { TERMINAL_JOB } from "./api";
import type { Job } from "./api";

export interface RunMetrics {
  net_pnl: number | null;
  total_return_pct: number | null;
  sharpe: number | null;
  max_drawdown_pct: number | null;
  win_rate_pct: number | null;
  total_trades: number | null;
}

export interface RobustnessResult {
  base: { run_id: string; metrics: RunMetrics; first_bar: string; last_bar: string };
  walk_forward: {
    mode: "folds" | "optimised";
    folds: { fold: number; start: string; end: string; run_id: string; metrics: RunMetrics;
      chosen?: string; in_sample_sharpe?: number | null }[];
    profitable_folds: number;
    efficiency?: number | null;
  };
  neighbourhood: { best: string; best_sharpe: number; neighbours: number; neighbour_median_sharpe: number | null;
    ratio: number | null; plateau_share: number } | null;
  slippage: { base_bps: number; breakeven_bps: number | null; survives_all: boolean; loses_at_base: boolean;
    levels: { multiplier: number; slippage_bps: number; run_id: string; metrics: RunMetrics }[] };
  second_instrument: { symbol: string; run_id: string; metrics: RunMetrics; synthetic_data?: boolean } | null;
  verdicts: string[];
  notes: string[];
  runs: number;
}

export interface ValidateOptions {
  folds: number;
  second_symbol?: string;
}

function fmt(n: number | null | undefined, digits = 2) {
  return n == null ? "–" : n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function tone(n: number | null | undefined) {
  return n == null ? "" : n > 0 ? "pos" : n < 0 ? "neg" : "";
}

/** The form under the Performance tab that starts a validation. */
export function ValidatePanel({ onValidate, running }: { onValidate: (o: ValidateOptions) => void; running: boolean }) {
  const [folds, setFolds] = useState(4);
  const [second, setSecond] = useState("");
  return (
    <div className="panel" style={{ marginTop: 12 }}>
      <div className="panel-title">Check robustness</div>
      <div className="row" style={{ gap: 8, alignItems: "flex-end" }}>
        <div className="field" style={{ marginBottom: 0 }}>
          <label htmlFor="val-folds">Walk-forward folds</label>
          <input id="val-folds" type="number" min={2} max={12} className="mono" value={folds}
            onChange={(e) => setFolds(Math.max(2, Math.min(12, Number(e.target.value) || 4)))} />
        </div>
        <div className="field" style={{ marginBottom: 0 }}>
          <label htmlFor="val-second">Second instrument (optional)</label>
          <input id="val-second" type="text" className="mono" value={second} placeholder="e.g. BANKNIFTY"
            onChange={(e) => setSecond(e.target.value)} />
        </div>
        <button className="btn" style={{ flex: "0 0 auto" }} disabled={running}
          onClick={() => onValidate({ folds, second_symbol: second.trim() || undefined })}>
          {running ? <><span className="spinner" /> Checking…</> : "Run checks"}
        </button>
      </div>
      <div className="hint">
        Walk-forward, neighbouring values (for a swept strategy), 1x to 5x slippage, and the other instrument.
        These runs check this strategy; they do not count as new tries of the idea.
      </div>
    </div>
  );
}

function MetricsCells({ m }: { m: RunMetrics }) {
  return (
    <>
      <td className={`mono ${tone(m.net_pnl)}`}>{fmt(m.net_pnl, 0)}</td>
      <td className="mono">{fmt(m.sharpe)}</td>
      <td className="mono">{fmt(m.max_drawdown_pct)}%</td>
      <td className="mono">{m.total_trades ?? "–"}</td>
    </>
  );
}

export function ValidationView({ job, onOpenRun, onCancel }: {
  job: Job<RobustnessResult>;
  onOpenRun: (runId: string) => void;
  onCancel: () => void;
}) {
  const running = !TERMINAL_JOB.includes(job.status);
  const r = job.status === "succeeded" ? job.result : null;
  const open = (id: string) => <button className="btn secondary tiny" onClick={() => onOpenRun(id)}>Report</button>;

  return (
    <div>
      {running && (
        <div className="panel" role="status">
          <div className="row" style={{ justifyContent: "space-between" }}>
            <span><span className="spinner" /> {job.message || "Starting the checks…"}</span>
            <button className="btn secondary tiny" style={{ flex: "0 0 auto" }} onClick={onCancel}>Cancel</button>
          </div>
          <div className="progress"><div style={{ width: `${Math.round(job.progress * 100)}%` }} /></div>
        </div>
      )}
      {job.status === "failed" && <div className="notice error"><div className="notice-title">Checks failed</div>{job.error}</div>}
      {job.status === "cancelled" && <div className="notice warn">Checks cancelled. The runs that finished are in the library.</div>}

      {r && (
        <>
          <div className="panel" aria-label="Verdicts">
            <div className="panel-title">
              <span>Robustness · {r.base.first_bar.slice(0, 10)} to {r.base.last_bar.slice(0, 10)}</span>
              <span className="hint">{r.runs} runs</span>
            </div>
            <ul>{r.verdicts.map((v, i) => <li key={i}>{v}</li>)}</ul>
          </div>

          <h3 className="section-title">Walk-forward{r.walk_forward.mode === "optimised" ? " (chosen in-sample, judged out-of-sample)" : ""}</h3>
          <div className="table-wrap">
            <table style={{ minWidth: 0 }}>
              <thead><tr><th>Fold</th><th>Dates</th>{r.walk_forward.mode === "optimised" && <><th>Chosen</th><th>In-sample Sharpe</th></>}
                <th>Net P&amp;L</th><th>Sharpe</th><th>Max DD</th><th>Trades</th><th /></tr></thead>
              <tbody>
                {r.walk_forward.folds.map((f) => (
                  <tr key={f.fold}>
                    <td>{f.fold}</td><td className="mono">{f.start} to {f.end}</td>
                    {r.walk_forward.mode === "optimised" && <><td className="mono">{f.chosen}</td><td className="mono">{fmt(f.in_sample_sharpe)}</td></>}
                    <MetricsCells m={f.metrics} /><td>{open(f.run_id)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {r.neighbourhood && (
            <>
              <h3 className="section-title">Neighbouring values</h3>
              <p>
                Best: <span className="mono">{r.neighbourhood.best}</span> (Sharpe {fmt(r.neighbourhood.best_sharpe)}).
                Its {r.neighbourhood.neighbours} neighbour{r.neighbourhood.neighbours === 1 ? "" : "s"} reach a median Sharpe of{" "}
                {fmt(r.neighbourhood.neighbour_median_sharpe)}
                {r.neighbourhood.ratio != null ? ` (${Math.round(r.neighbourhood.ratio * 100)}% of the best)` : ""};{" "}
                {Math.round(r.neighbourhood.plateau_share * 100)}% of the grid is within half of the best.
              </p>
            </>
          )}

          <h3 className="section-title">Slippage stress</h3>
          <div className="table-wrap">
            <table style={{ minWidth: 0 }}>
              <thead><tr><th>Slippage</th><th>Net P&amp;L</th><th>Sharpe</th><th>Max DD</th><th>Trades</th><th /></tr></thead>
              <tbody>
                {r.slippage.levels.map((l) => (
                  <tr key={l.slippage_bps}>
                    <td className="mono">{l.slippage_bps} bps ({l.multiplier}x)</td><MetricsCells m={l.metrics} /><td>{open(l.run_id)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {r.slippage.breakeven_bps != null && <p className="hint">Profit is gone at about {r.slippage.breakeven_bps} bps.</p>}

          {r.second_instrument && (
            <>
              <h3 className="section-title">On {r.second_instrument.symbol}</h3>
              <div className="table-wrap">
                <table style={{ minWidth: 0 }}>
                  <thead><tr><th>Instrument</th><th>Net P&amp;L</th><th>Sharpe</th><th>Max DD</th><th>Trades</th><th /></tr></thead>
                  <tbody>
                    <tr><td>Tested</td><MetricsCells m={r.base.metrics} /><td>{open(r.base.run_id)}</td></tr>
                    <tr><td>{r.second_instrument.symbol}</td><MetricsCells m={r.second_instrument.metrics} /><td>{open(r.second_instrument.run_id)}</td></tr>
                  </tbody>
                </table>
              </div>
            </>
          )}

          <ul className="caveats" style={{ marginTop: 10 }}>{r.notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
        </>
      )}
    </div>
  );
}
