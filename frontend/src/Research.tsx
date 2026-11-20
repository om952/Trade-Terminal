// Research runs: the model proposes structural variants of a strategy, the
// trader approves the plan and its cost, every candidate is backtested and
// judged by code (deflated by the whole search, then walk-forward and slippage
// checks), and the trader keeps the survivors they want.

import { useEffect, useState } from "react";
import { api, ApiError } from "./api";
import type { ResearchCandidate, ResearchSession } from "./api";

type Doc = Record<string, any>;

export interface ResearchRunSettings {
  symbol: string;
  starting_capital: number;
  lot_size: number;
  slippage_bps: number;
}

/** Sidebar: start a run on the strategy in the builder, or reopen one. */
export function ResearchPanel({ doc, settings, sessionId, onSession }: {
  doc: Doc;
  settings: ResearchRunSettings;
  sessionId: string | null;
  onSession: (s: ResearchSession | null) => void;
}) {
  const [goal, setGoal] = useState("");
  const [rows, setRows] = useState<{ id: string; title: string; instrument: string }[]>([]);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const refresh = async () => {
    try { setRows((await api.researchList()).sessions); } catch { /* backend down */ }
  };
  useEffect(() => { refresh(); }, [sessionId]);

  const start = async () => {
    setBusy(true);
    setProblem(null);
    try {
      onSession(await api.researchStart({
        strategy: doc, goal: goal.trim(), symbol: settings.symbol || undefined,
        starting_capital: settings.starting_capital, lot_size: settings.lot_size, slippage_bps: settings.slippage_bps,
      }));
      setGoal("");
    } catch (e) {
      setProblem((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="panel">
        <div className="panel-title">Research runs</div>
        <select className="mono" aria-label="Research run" value={sessionId ?? ""}
          onChange={async (e) => onSession(e.target.value ? await api.research(e.target.value) : null)}>
          <option value="">— new run —</option>
          {rows.map((r) => <option key={r.id} value={r.id}>{r.title} · {r.instrument}</option>)}
        </select>
      </div>

      <div className="panel">
        <div className="panel-title">New run</div>
        <div className="hint" style={{ marginBottom: 6 }}>
          Base: <b>{doc.name || "the strategy in the builder"}</b> on {settings.symbol || doc.instrument?.symbol || "its instrument"}, {doc.timeframe}.
        </div>
        <div className="field">
          <label htmlFor="research-goal">What should the variants try?</label>
          <textarea id="research-goal" rows={3} value={goal} onChange={(e) => setGoal(e.target.value)}
            placeholder="e.g. Fewer false signals in sideways markets; try trend and volatility filters." />
        </div>
        <button className="btn" style={{ width: "100%" }} disabled={busy} onClick={start}>
          {busy ? <><span className="spinner" /> Starting…</> : "Plan research"}
        </button>
        {problem && <div className="notice error">{problem}</div>}
        <div className="hint" style={{ marginTop: 6 }}>
          One model call plans up to eight structurally different variants. Nothing is backtested until you
          approve the plan and its cost, and code, not the model, judges the results.
        </div>
      </div>
    </>
  );
}

function pct(x: number | null | undefined) {
  return x == null ? "–" : `${Math.round(x * 100)}%`;
}

function fmt(x: number | null | undefined, digits = 2) {
  return x == null ? "–" : x.toFixed(digits);
}

/** Main area: the run's plan, results and what was kept. */
export function ResearchView({ session, onReply, onOpenRun, onOpenStrategy }: {
  session: ResearchSession;
  onReply: (payload: Record<string, unknown>) => void;
  onOpenRun: (runId: string) => void;
  onOpenStrategy: (strategyId: string) => void;
}) {
  const [drop, setDrop] = useState<Set<string>>(new Set());
  const [keep, setKeep] = useState<Set<string>>(new Set());
  useEffect(() => { setDrop(new Set()); setKeep(new Set()); }, [session.id, session.phase]);

  const toggle = (set: Set<string>, id: string, on: boolean) => {
    const next = new Set(set);
    if (on) next.add(id); else next.delete(id);
    return next;
  };

  const judged = session.events.filter((e) => e.kind === "judged").at(-1);
  const accepted = session.proposals.filter((p) => p.status === "accepted");

  return (
    <div>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "baseline", marginBottom: 8 }}>
        <b style={{ flex: "1 1 auto" }}>{session.title}</b>
        <span className="hint" style={{ flex: "0 0 auto" }}>{session.instrument} · {session.regime ?? "–"}</span>
      </div>

      {session.phase === "working" && session.job && (
        <div className="panel" role="status">
          <span><span className="spinner" /> {session.job.message || "Working…"}</span>
          <div className="progress"><div style={{ width: `${Math.round(session.job.progress * 100)}%` }} /></div>
        </div>
      )}
      {session.phase === "failed" && <div className="notice error">{session.error ?? session.job?.error}</div>}
      {session.phase === "interrupted" && <div className="notice warn">This run was cut off: {session.job?.error ?? "the app stopped"}.</div>}
      {session.phase === "rejected" && <div className="notice">The plan was rejected; nothing was backtested.</div>}

      {session.proposals.length > 0 && (
        <>
          <h3 className="section-title">The plan</h3>
          <div className="table-wrap">
            <table style={{ minWidth: 0 }} aria-label="Research plan">
              <thead><tr><th>Run</th><th>Hypothesis</th><th>Change</th><th>Status</th></tr></thead>
              <tbody>
                {session.proposals.map((p) => (
                  <tr key={p.id}>
                    <td>
                      {p.status === "accepted" && session.phase === "awaiting_plan_approval" ? (
                        <input type="checkbox" style={{ width: "auto" }} aria-label={`Run ${p.hypothesis}`}
                          checked={!drop.has(p.id)} onChange={(e) => setDrop(toggle(drop, p.id, !e.target.checked))} />
                      ) : p.status === "accepted" ? "✓" : ""}
                    </td>
                    <td>{p.hypothesis}</td>
                    <td className="hint">{p.change_summary}</td>
                    <td className={p.status === "accepted" ? "pos" : "hint"}>{p.status === "accepted" ? "accepted" : `${p.status}: ${p.reason}`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {session.phase === "awaiting_plan_approval" && session.estimate && (
        <div className="panel" style={{ marginTop: 10 }} aria-label="Cost">
          <div className="panel-title">Cost</div>
          <p>
            {accepted.length - drop.size + 1} candidates (the base and {accepted.length - drop.size} variant{accepted.length - drop.size === 1 ? "" : "s"}):{" "}
            {accepted.length - drop.size + 1} backtests now, up to {(accepted.length - drop.size + 1) * (session.estimate.backtests_max / session.estimate.candidates)}{" "}
            if every one clears the deflated Sharpe bar and gets the robustness checks (cap {session.estimate.max_backtests}).
            Model: {session.estimate.model_calls_used} call ({session.estimate.model_tokens_used.toLocaleString()} tokens) used; no more to come.
          </p>
          <div className="row" style={{ gap: 6 }}>
            <button className="btn" style={{ flex: "0 0 auto" }} disabled={accepted.length - drop.size < 1}
              onClick={() => onReply({ action: "approve", drop: [...drop] })}>Approve and run</button>
            <button className="btn danger" style={{ flex: "0 0 auto" }} onClick={() => onReply({ action: "reject" })}>Reject</button>
          </div>
        </div>
      )}

      {session.ranking.length > 0 && (
        <>
          <h3 className="section-title">Results</h3>
          {judged && <p>{judged.text}. {(judged.data?.notes as string[] | undefined)?.join(" ")}</p>}
          <div className="table-wrap">
            <table style={{ minWidth: 0 }} aria-label="Research results">
              <thead><tr><th>Keep</th><th>Candidate</th><th>Sharpe</th><th>Deflated</th><th>Checks</th><th>Outcome</th><th /></tr></thead>
              <tbody>
                {session.ranking.map((c: ResearchCandidate) => (
                  <tr key={c.id}>
                    <td>
                      {c.outcome === "survived" && session.phase === "awaiting_choice" && (
                        <input type="checkbox" style={{ width: "auto" }} aria-label={`Keep ${c.hypothesis}`}
                          checked={keep.has(c.id)} onChange={(e) => setKeep(toggle(keep, c.id, e.target.checked))} />
                      )}
                    </td>
                    <td>{c.hypothesis}</td>
                    <td className="mono">{fmt(c.metrics?.sharpe)}</td>
                    <td className="mono" title={c.deflated ? `${c.deflated.trials} tries; best expected by chance ${fmt(c.deflated.benchmark_sharpe)}` : ""}>{pct(c.dsr)}</td>
                    <td className="mono">{c.checks ? `${c.checks.profitable_folds}/${c.checks.folds} folds · ${c.checks.survives_slippage ? "2x slippage ok" : "fails 2x slippage"}` : "–"}</td>
                    <td className={c.outcome === "survived" ? "pos" : "hint"}>{c.outcome}: {c.reason}</td>
                    <td>{c.run_id && <button className="btn secondary tiny" onClick={() => onOpenRun(c.run_id!)}>Report</button>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {session.phase === "awaiting_choice" && (
            <div className="row" style={{ gap: 6, marginTop: 8 }}>
              <button className="btn" style={{ flex: "0 0 auto" }} onClick={() => onReply({ keep: [...keep] })}>
                {keep.size ? `Keep ${keep.size} in the library` : session.ranking.some((c) => c.outcome === "survived") ? "Keep none" : "Finish"}
              </button>
              {!session.ranking.some((c) => c.outcome === "survived") && (
                <span className="hint">No candidate survived. That is the expected outcome for most ideas, and it is recorded so the next plan avoids them.</span>
              )}
            </div>
          )}
        </>
      )}

      {session.phase === "done" && (
        <div className="panel" style={{ marginTop: 10 }}>
          <div className="panel-title">Done</div>
          {session.kept.length === 0 ? <p>Nothing kept. The outcomes are in research memory.</p> : (
            <ul>
              {session.kept.map((k) => {
                const c = session.ranking.find((r) => r.id === k.id);
                return (
                  <li key={k.id}>
                    {c?.hypothesis} saved as version {k.version}{" "}
                    <button className="btn secondary tiny" onClick={() => onOpenStrategy(k.strategy_id)}>Open in builder</button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

export async function openSaved(strategyId: string): Promise<Doc> {
  const saved = await api.strategy(strategyId);
  return saved.document as Doc;
}
