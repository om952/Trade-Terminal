// Sweeps: editing a strategy's `sweep`, and showing what a grid of backtests did.
//
// The table lists every variant in grid order and highlights none of them. The
// top row of a grid is partly luck, and every variant counts as a trial of the
// idea, so the page shows the spread and the trial count rather than a winner.

import { useEffect, useMemo, useState } from "react";
import type { SweepResult, SweepRow } from "./api";

type Doc = Record<string, any>;

export const MAX_SWEEP_RUNS = 200;
export const MAX_SWEEP_AXES = 4;

/** Number of backtests a document's sweep would run (0 when it has none). */
export function sweepCount(doc: Doc | null | undefined): number {
  const axes: { values?: unknown[] }[] = Array.isArray(doc?.sweep) ? doc!.sweep : [];
  if (axes.length === 0) return 0;
  return axes.reduce((n, a) => n * (Array.isArray(a.values) ? a.values.length : 0), 1);
}

/** "/indicators/0/params/period" -> "ema_fast.period", as the backend labels it. */
export function pathLabel(path: string, doc: Doc): string {
  let tokens = path.replace(/^\//, "").split("/");
  if (tokens.length > 1 && (tokens[0] === "indicators" || tokens[0] === "legs") && /^\d+$/.test(tokens[1])) {
    const id = doc?.[tokens[0]]?.[Number(tokens[1])]?.id;
    if (id) tokens = [id, ...tokens.slice(2)];
  }
  return tokens.filter((t) => t !== "params").join(".");
}

/** Every number written in the document, as JSON Pointers a sweep may vary. */
export function numericPaths(doc: Doc): string[] {
  const out: string[] = [];
  const walk = (node: unknown, path: string) => {
    if (typeof node === "number") { out.push(path); return; }
    if (Array.isArray(node)) { node.forEach((v, i) => walk(v, `${path}/${i}`)); return; }
    if (node && typeof node === "object") {
      for (const [k, v] of Object.entries(node)) {
        if (path === "" && (k === "sweep" || k === "schema_version")) continue;
        walk(v, `${path}/${k.replace(/~/g, "~0").replace(/\//g, "~1")}`);
      }
    }
  };
  walk(doc, "");
  return out;
}

/** "5, 9, 13" or "5..20 step 5" -> numbers, or an error message. */
export function parseValues(raw: string): number[] | string {
  const text = raw.trim();
  const range = /^(-?[\d.]+)\s*\.\.\s*(-?[\d.]+)(?:\s+step\s+([\d.]+))?$/i.exec(text);
  if (range) {
    const [from, to, step] = [Number(range[1]), Number(range[2]), Number(range[3] ?? 1)];
    if (![from, to, step].every(Number.isFinite) || step <= 0 || to < from) {
      return "A range is 'from..to step n', with to at least from and n above 0.";
    }
    const values: number[] = [];
    for (let i = 0; from + i * step <= to + 1e-9; i++) {
      values.push(Number((from + i * step).toFixed(10)));
      if (values.length > 50) return "At most 50 values per parameter.";
    }
    return values;
  }
  const parts = text.split(/[,\s]+/).filter(Boolean);
  const values = parts.map(Number);
  if (parts.length === 0 || values.some((v) => !Number.isFinite(v))) {
    return "List numbers separated by commas, or a range like 5..20 step 5.";
  }
  return values;
}

/* ------------------------------------------------------------------ */

export function SweepEditor({ doc, setDoc }: { doc: Doc; setDoc: (d: Doc) => void }) {
  const axes: { path: string; values: number[] }[] = Array.isArray(doc.sweep) ? doc.sweep : [];
  const paths = useMemo(() => numericPaths(doc), [doc]);
  const free = paths.filter((p) => !axes.some((a) => a.path === p));
  const total = sweepCount(doc);

  const setAxes = (next: { path: string; values: number[] }[]) => {
    const copy = { ...doc };
    if (next.length) copy.sweep = next;
    else delete copy.sweep;
    setDoc(copy);
  };

  const add = (path: string) => {
    if (!path) return;
    const current = path.split("/").slice(1).reduce((n: any, k) => n?.[k], doc);
    const base = typeof current === "number" ? current : 1;
    // Start with the current value and two neighbours; the trader edits from there.
    const step = Number.isInteger(base) ? Math.max(1, Math.round(Math.abs(base) * 0.25)) : Math.abs(base) * 0.25 || 0.5;
    const values = [base, base + step, base + 2 * step].map((v) => Number(v.toFixed(6)));
    setAxes([...axes, { path, values }]);
  };

  return (
    <div className="panel">
      <div className="panel-title">
        <span>Sweep</span>
        {total > 0 && (
          <span className={total > MAX_SWEEP_RUNS ? "neg" : "hint"}>
            {total} backtest{total === 1 ? "" : "s"}
          </span>
        )}
      </div>

      {axes.map((axis, i) => (
        <AxisRow
          key={axis.path}
          label={pathLabel(axis.path, doc)}
          values={axis.values}
          onChange={(values) => setAxes(axes.map((a, j) => (j === i ? { ...a, values } : a)))}
          onRemove={() => setAxes(axes.filter((_, j) => j !== i))}
        />
      ))}

      {axes.length < MAX_SWEEP_AXES && (
        <select className="mono" value="" onChange={(e) => add(e.target.value)} aria-label="Add a parameter to sweep">
          <option value="">+ vary a number…</option>
          {free.map((p) => <option key={p} value={p}>{pathLabel(p, doc)}</option>)}
        </select>
      )}

      <div className="hint" style={{ marginTop: 6 }}>
        {total > MAX_SWEEP_RUNS
          ? `At most ${MAX_SWEEP_RUNS} backtests per sweep: use fewer values.`
          : axes.length
            ? "Every combination runs as its own backtest and counts as a trial of this idea, so the best row is partly luck."
            : "Optional. Try a range of values for any number, each combination as its own backtest."}
      </div>
    </div>
  );
}

function AxisRow({ label, values, onChange, onRemove }: {
  label: string; values: number[]; onChange: (v: number[]) => void; onRemove: () => void;
}) {
  const [draft, setDraft] = useState(values.join(", "));
  const [problem, setProblem] = useState<string | null>(null);

  // A change from outside (a template, the assistant) replaces the draft.
  useEffect(() => { setDraft(values.join(", ")); setProblem(null); }, [values.join(",")]);

  const commit = (raw: string) => {
    setDraft(raw);
    const parsed = parseValues(raw);
    if (typeof parsed === "string") { setProblem(parsed); return; }
    setProblem(null);
    onChange(parsed);
  };

  return (
    <div className="rule">
      <div className="rule-head">
        <span className="rule-label mono">{label}</span>
        <button className="remove" onClick={onRemove} title="Stop varying this">×</button>
      </div>
      <input type="text" className="mono" value={draft} aria-label={`Values for ${label}`}
        onChange={(e) => commit(e.target.value)} placeholder="5, 9, 13 or 5..20 step 5" />
      {problem && <span className="hint neg">{problem}</span>}
    </div>
  );
}

/* ------------------------------------------------------------------ */

function fmt(n: number | null | undefined, digits = 2) {
  return n == null ? "–" : n.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
}

function signed(n: number | null | undefined) {
  return n == null ? "" : n > 0 ? "pos" : n < 0 ? "neg" : "";
}

export function SweepTable({ sweep, onOpenRun, onUseRun, compact }: {
  sweep: SweepResult;
  onOpenRun: (runId: string) => void;
  onUseRun?: (runId: string) => void;
  compact?: boolean;
}) {
  const pnl = sweep.variants.map((r) => r.metrics.net_pnl).filter((v): v is number => v != null);
  const profitable = pnl.filter((v) => v > 0).length;
  const trials = sweep.family_trials?.runs;

  return (
    <div className={compact ? "card result-card" : ""}>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 6 }}>
        <b style={{ flex: "1 1 auto" }}>Sweep · {sweep.symbol ?? ""} {sweep.timeframe} · {sweep.variants.length} variants</b>
        {sweep.synthetic_data && <span className="tag warn" style={{ flex: "0 0 auto" }}>synthetic data</span>}
      </div>

      {pnl.length > 0 && (
        <p className="hint">
          Net P&amp;L from {fmt(Math.min(...pnl), 0)} to {fmt(Math.max(...pnl), 0)} · {profitable} of {pnl.length} made money
          {trials ? ` · ${trials} backtests of this idea so far` : ""}
        </p>
      )}

      <div className={`table-wrap ${compact ? "table-scroll" : ""}`}>
        <table style={{ minWidth: 0 }}>
          <thead>
            <tr>
              {sweep.axes.map((a) => <th key={a.path}>{a.label}</th>)}
              <th>Trades</th><th>Net P&amp;L</th><th>Return</th><th>Max DD</th>
              {!compact && <><th>Win rate</th><th>Sharpe</th><th>PF</th></>}
              <th />
            </tr>
          </thead>
          <tbody>
            {sweep.variants.map((row) => (
              <VariantRow key={row.run_id} row={row} axes={sweep.axes.map((a) => a.path)}
                compact={compact} onOpenRun={onOpenRun} onUseRun={onUseRun} />
            ))}
          </tbody>
        </table>
      </div>

      {sweep.skipped.length > 0 && (
        <details style={{ marginTop: 6 }}>
          <summary className="hint">{sweep.skipped.length} skipped as not valid</summary>
          <ul className="caveats">
            {sweep.skipped.map((s, i) => <li key={i}><code>{s.label}</code>: {s.reason}</li>)}
          </ul>
        </details>
      )}

      <p className="hint" style={{ marginTop: 6 }}>
        {sweep.note} A value whose neighbours also hold up is more believable than a lone peak.
      </p>
    </div>
  );
}

function VariantRow({ row, axes, compact, onOpenRun, onUseRun }: {
  row: SweepRow; axes: string[]; compact?: boolean;
  onOpenRun: (id: string) => void; onUseRun?: (id: string) => void;
}) {
  const m = row.metrics;
  return (
    <tr>
      {axes.map((p) => <td key={p} className="mono">{row.assignment[p]}</td>)}
      <td>{m.total_trades ?? "–"}</td>
      <td className={signed(m.net_pnl)}>{fmt(m.net_pnl, 0)}</td>
      <td className={signed(m.total_return_pct)}>{fmt(m.total_return_pct)}%</td>
      <td>{fmt(m.max_drawdown_pct)}%</td>
      {!compact && <><td>{fmt(m.win_rate_pct, 1)}%</td><td>{fmt(m.sharpe)}</td><td>{fmt(m.profit_factor)}</td></>}
      <td style={{ whiteSpace: "nowrap" }}>
        <button className="btn secondary tiny" onClick={() => onOpenRun(row.run_id)}>Report</button>
        {onUseRun && (
          <button className="btn secondary tiny" style={{ marginLeft: 4 }}
            title="Load this variant into the builder, without the sweep"
            onClick={() => onUseRun(row.run_id)}>Use</button>
        )}
      </td>
    </tr>
  );
}
