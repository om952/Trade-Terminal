// The strategy builder chat: describe a strategy, answer at most two questions,
// approve a read-back, see a backtest, refine. All decisions happen on the
// server; this component renders the conversation and sends replies.

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";
import type {
  BuilderBacktestSettings,
  Conversation,
  ConversationRow,
  LLMStatus,
  Pending,
  Question,
  ReadbackCard,
  RunSummary,
  SweepResult,
  TranscriptEvent,
} from "./api";
import { SweepTable, sweepCount } from "./Sweep";

const POLL_MS = 1000;

export interface AssistantProps {
  onOpenRun: (runId: string) => void;
  onStartPaper: (document: Record<string, unknown>, run: RunSummary) => void;
  onOpenDocument: (document: Record<string, unknown>, strategyId: string | null) => void;
}

export function Assistant({ onOpenRun, onStartPaper, onOpenDocument }: AssistantProps) {
  const [llm, setLlm] = useState<LLMStatus | null>(null);
  const [rows, setRows] = useState<ConversationRow[]>([]);
  const [conv, setConv] = useState<Conversation | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  const refreshList = useCallback(async () => {
    try { setRows((await api.conversations()).conversations); } catch { /* backend down */ }
  }, []);

  useEffect(() => {
    api.llmStatus().then(setLlm).catch(() => setLlm(null));
    refreshList();
  }, [refreshList]);

  // Poll while a turn runs on the server.
  useEffect(() => {
    if (!conv || conv.phase !== "working") return;
    const timer = setTimeout(async () => {
      try {
        const next = await api.conversation(conv.id);
        setConv(next);
        if (next.phase !== "working") refreshList();
      } catch (e) {
        setError((e as ApiError).message);
      }
    }, POLL_MS);
    return () => clearTimeout(timer);
  }, [conv, refreshList]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView?.({ block: "end" });
  }, [conv?.transcript.length, conv?.phase]);

  const act = async (fn: () => Promise<Conversation>) => {
    setBusy(true);
    setError(null);
    try {
      setConv(await fn());
    } catch (e) {
      setError((e as ApiError).message || "Request failed");
    } finally {
      setBusy(false);
    }
  };

  const start = (text: string, settings: BuilderBacktestSettings) =>
    act(() => api.startConversation(text, settings)).then(refreshList);
  const reply = (payload: Record<string, unknown>) => act(() => api.reply(conv!.id, payload));

  if (llm && !llm.configured) {
    return <KeySetup onSaved={setLlm} model={llm.model} />;
  }

  const composerMode =
    !conv || ["failed", "unsupported", "rejected"].includes(conv.phase) ? "start"
      : conv.phase === "awaiting_followup" ? "followup" : null;

  return (
    <div className="assistant">
      <div className="panel">
        <div className="panel-title">
          <span>Strategy assistant</span>
          {llm && <span className="hint">{llm.model}</span>}
        </div>
        <div className="row" style={{ gap: 6 }}>
          <select
            className="mono"
            value={conv?.id ?? ""}
            onChange={(e) => e.target.value
              ? act(() => api.conversation(e.target.value))
              : setConv(null)}
          >
            <option value="">New conversation</option>
            {rows.map((r) => <option key={r.id} value={r.id}>{r.title}</option>)}
          </select>
          {conv && <button className="btn secondary tiny" onClick={() => setConv(null)}>New</button>}
        </div>
      </div>

      {conv && (
        <div className="chat" aria-live="polite">
          {conv.transcript.map((event, i) => (
            <TranscriptItem
              key={i}
              event={event}
              document={conv.document}
              onOpenRun={onOpenRun}
              onStartPaper={onStartPaper}
            />
          ))}

          {conv.phase === "working" && conv.job && (
            <Progress
              message={conv.job.message || "Working…"}
              progress={conv.job.progress}
              onCancel={() => act(() => api.cancelTurn(conv.id))}
            />
          )}

          {conv.pending && conv.phase !== "working" && (
            <PendingView pending={conv.pending} busy={busy} onReply={reply}
              onOpenDocument={(d) => onOpenDocument(d, conv.strategy_id)} />
          )}

          {conv.phase === "interrupted" && (
            <div className="notice warn">
              <div className="notice-title">This turn was cut off</div>
              {conv.job?.error && <div>{conv.job.error}</div>}
              <button className="btn tiny" disabled={busy} onClick={() => act(() => api.retryTurn(conv.id))}>
                Continue where it stopped
              </button>
            </div>
          )}

          <Usage conv={conv} />
          <div ref={bottomRef} />
        </div>
      )}

      {error && <div className="notice error"><div className="notice-title">Error</div>{error}</div>}

      {composerMode && (
        <Composer
          mode={composerMode}
          busy={busy}
          onSubmit={(text, settings) => composerMode === "start" ? start(text, settings) : reply({ text })}
        />
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */

function KeySetup({ onSaved, model }: { onSaved: (s: LLMStatus) => void; model: string }) {
  const [key, setKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const save = async () => {
    setError(null);
    try {
      onSaved(await api.setLlmKey(key.trim()));
      setKey("");
    } catch (e) {
      setError((e as ApiError).message);
    }
  };
  return (
    <div className="panel">
      <div className="panel-title">Connect the strategy assistant</div>
      <p className="hint">
        The assistant uses {model}. Paste an Anthropic API key. It is stored in your
        operating system's keychain, never in a file, and is not shown again.
      </p>
      <div className="field">
        <label htmlFor="llm-key">Anthropic API key</label>
        <input id="llm-key" type="password" className="mono" value={key}
          autoComplete="off" onChange={(e) => setKey(e.target.value)} />
      </div>
      {error && <div className="notice error">{error}</div>}
      <button className="btn" disabled={key.trim().length < 20} onClick={save}>Save key</button>
    </div>
  );
}

function Composer({ mode, busy, onSubmit }: {
  mode: "start" | "followup";
  busy: boolean;
  onSubmit: (text: string, settings: BuilderBacktestSettings) => void;
}) {
  const [text, setText] = useState("");
  const [settings, setSettings] = useState<BuilderBacktestSettings>({ lot_size: 1, starting_capital: 1_000_000 });
  const submit = () => {
    if (!text.trim()) return;
    onSubmit(text.trim(), settings);
    setText("");
  };
  return (
    <div className="panel">
      <div className="field">
        <label htmlFor="composer">{mode === "start" ? "Describe a strategy" : "Ask for a change"}</label>
        <textarea
          id="composer"
          rows={mode === "start" ? 5 : 2}
          value={text}
          placeholder={mode === "start"
            ? "e.g. On BANKNIFTY 5-minute, sell the ATM put when the 9 EMA crosses above the 50 SMA and RSI is above 50. 30% stop on the premium, out by 15:10."
            : "e.g. Tighten the stop to 20% and allow at most two trades a day."}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit(); }}
        />
      </div>
      {mode === "start" && (
        <div className="field-row">
          <div className="field">
            <label htmlFor="bt-from">Backtest from</label>
            <input id="bt-from" type="date" value={settings.start ?? ""}
              onChange={(e) => setSettings({ ...settings, start: e.target.value || undefined })} />
          </div>
          <div className="field">
            <label htmlFor="bt-to">to</label>
            <input id="bt-to" type="date" value={settings.end ?? ""}
              onChange={(e) => setSettings({ ...settings, end: e.target.value || undefined })} />
          </div>
          <div className="field">
            <label htmlFor="bt-lots">Lot size</label>
            <input id="bt-lots" type="number" min={1} value={settings.lot_size ?? 1}
              onChange={(e) => setSettings({ ...settings, lot_size: Number(e.target.value) || 1 })} />
          </div>
        </div>
      )}
      <button className="btn" style={{ width: "100%" }} disabled={busy || !text.trim()} onClick={submit}>
        {busy ? <><span className="spinner" /> Sending…</> : mode === "start" ? "Build strategy" : "Send"}
      </button>
      {mode === "start" && <p className="hint">Nothing runs until you approve the read-back. Data is downloaded automatically.</p>}
    </div>
  );
}

function Progress({ message, progress, onCancel }: { message: string; progress: number; onCancel: () => void }) {
  return (
    <div className="bubble assistant" role="status">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <span><span className="spinner" /> {message}</span>
        <button className="btn secondary tiny" onClick={onCancel}>Cancel</button>
      </div>
      <div className="progress"><div style={{ width: `${Math.round(progress * 100)}%` }} /></div>
    </div>
  );
}

function Usage({ conv }: { conv: Conversation }) {
  const u = conv.usage;
  if (!u.steps) return null;
  return (
    <p className="hint">
      This turn: {u.steps}/{u.limits.steps} model calls · {(u.total ?? 0).toLocaleString()} tokens ·{" "}
      {Math.round(u.seconds ?? 0)}s of {u.limits.seconds}s
    </p>
  );
}

/* ------------------------------------------------------------------ */

function TranscriptItem({ event, document, onOpenRun, onStartPaper }: {
  event: TranscriptEvent;
  document: Record<string, unknown> | null;
  onOpenRun: (id: string) => void;
  onStartPaper: AssistantProps["onStartPaper"];
}) {
  switch (event.kind) {
    case "text":
    case "answers":
      return <div className={`bubble ${event.role}`}>{event.text}</div>;
    case "questions":
      return (
        <div className="bubble assistant">
          {(event.data?.questions as Question[] | undefined)?.map((q, i) => <div key={i}>{q.question}</div>)}
        </div>
      );
    case "approval":
      return <div className="bubble user">Approved</div>;
    case "reject":
      return <div className="bubble user">Rejected</div>;
    case "edit":
      return <div className="bubble user">{event.text}</div>;
    case "change":
      return <div className="bubble assistant">Change: {event.text}</div>;
    case "saved":
      return <p className="hint">{event.text}</p>;
    case "result":
      return <ResultCard run={event.data!.run as RunSummary} document={document}
        onOpenRun={onOpenRun} onStartPaper={onStartPaper} />;
    case "sweep":
      return <SweepTable sweep={event.data!.sweep as SweepResult} onOpenRun={onOpenRun} compact />;
    case "explanation":
      return (
        <div className="bubble assistant">
          <div>{event.text}</div>
          {(event.data?.caveats as string[] | undefined)?.length ? (
            <ul className="caveats">
              {(event.data!.caveats as string[]).map((c, i) => <li key={i}>{c}</li>)}
            </ul>
          ) : null}
        </div>
      );
    case "error":
    case "unsupported":
      return <div className="notice error">{event.text}</div>;
    default:
      return event.text ? <div className={`bubble ${event.role}`}>{event.text}</div> : null;
  }
}

function money(n: number | null | undefined) {
  return n == null ? "–" : n.toLocaleString(undefined, { maximumFractionDigits: 0 });
}

function ResultCard({ run, document, onOpenRun, onStartPaper }: {
  run: RunSummary;
  document: Record<string, unknown> | null;
  onOpenRun: (id: string) => void;
  onStartPaper: AssistantProps["onStartPaper"];
}) {
  const m = run.metrics;
  return (
    <div className="card result-card">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <b>Backtest · {run.symbol} {run.timeframe}</b>
        {run.synthetic_data && <span className="tag warn">synthetic data</span>}
      </div>
      <div className="metric-grid compact">
        <Metric label="Net P&L" value={money(m.net_pnl)} sign={m.net_pnl} />
        <Metric label="Return" value={`${m.total_return_pct?.toFixed(2)}%`} sign={m.total_return_pct} />
        <Metric label="Max DD" value={`${m.max_drawdown_pct?.toFixed(2)}%`} />
        <Metric label="Trades" value={String(run.trades)} />
        <Metric label="Win rate" value={`${m.win_rate_pct?.toFixed(1)}%`} />
        <Metric label="Sharpe" value={m.sharpe?.toFixed(2) ?? "–"} />
      </div>
      <p className="hint">
        {run.data.source}{run.data.derived_from ? `, derived from ${run.data.derived_from}` : ""} ·{" "}
        {run.data.first_bar?.slice(0, 10)} to {run.data.last_bar?.slice(0, 10)} ·{" "}
        {run.data.bars.toLocaleString()} bars · data {run.data.fingerprint.slice(0, 8)}
        {run.data.missing_sessions ? ` · ${run.data.missing_sessions} missing session(s)` : ""}
        {run.trials > 1 ? ` · backtest #${run.trials} of this exact strategy` : ""}
      </p>
      {run.family_trials && run.family_trials.runs > 1 && (
        <p className="hint" title="Retuning numbers, times, strikes, the instrument or the timeframe keeps a strategy in the same family. The more variants tried, the more a good backtest owes to luck.">
          {run.family_trials.runs} backtests across {run.family_trials.variants} variant
          {run.family_trials.variants === 1 ? "" : "s"} of this idea so far. The more variants tried,
          the more of the best result is luck.
        </p>
      )}
      {run.deflated && run.deflated.dsr != null && (
        <p className="hint" title="Deflated Sharpe ratio: the chance the edge is real after every distinct test of this idea.">
          Deflated Sharpe {Math.round(run.deflated.dsr * 100)}% after {run.deflated.trials} tr{run.deflated.trials === 1 ? "y" : "ies"}: {run.deflated.verdict}.
        </p>
      )}
      {run.warnings.length > 0 && (
        <ul className="caveats">{run.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>
      )}
      <div className="row" style={{ gap: 6 }}>
        {run.run_id && <button className="btn secondary tiny" onClick={() => onOpenRun(run.run_id!)}>Open full report</button>}
        {document && <button className="btn tiny" onClick={() => onStartPaper(document, run)}>Start paper trading</button>}
      </div>
    </div>
  );
}

function Metric({ label, value, sign }: { label: string; value: string; sign?: number }) {
  const cls = sign == null ? "" : sign > 0 ? "pos" : sign < 0 ? "neg" : "";
  return (
    <div className="metric">
      <div className="metric-label">{label}</div>
      <div className={`metric-value ${cls}`}>{value}</div>
    </div>
  );
}

/* ------------------------------------------------------------------ */

function PendingView({ pending, busy, onReply, onOpenDocument }: {
  pending: Pending;
  busy: boolean;
  onReply: (payload: Record<string, unknown>) => void;
  onOpenDocument: (d: Record<string, unknown>) => void;
}) {
  if (pending.type === "questions") return <Questions questions={pending.questions} busy={busy} onReply={onReply} />;
  if (pending.type === "approval") {
    return <Approval card={pending.card} assumptions={pending.assumptions} document={pending.document}
      busy={busy} onReply={onReply} onOpenDocument={onOpenDocument} />;
  }
  return null;
}

function Questions({ questions, busy, onReply }: {
  questions: Question[];
  busy: boolean;
  onReply: (payload: Record<string, unknown>) => void;
}) {
  const [answers, setAnswers] = useState<string[]>(questions.map(() => ""));
  const set = (i: number, v: string) => setAnswers(answers.map((a, j) => (j === i ? v : a)));
  return (
    <div className="card">
      {questions.map((q, i) => (
        <div key={i} className="field">
          <label htmlFor={`q-${i}`}>{q.question}</label>
          <span className="hint">{q.why}</span>
          <div className="chip-row">
            {q.options.map((o) => (
              <button key={o} className={`chip ${answers[i] === o ? "active" : ""}`} onClick={() => set(i, o)}>{o}</button>
            ))}
          </div>
          <input id={`q-${i}`} value={answers[i]} placeholder="or type an answer" onChange={(e) => set(i, e.target.value)} />
        </div>
      ))}
      <button className="btn" disabled={busy || answers.some((a) => !a.trim())}
        onClick={() => onReply({ answers: answers.map((a) => a.trim()) })}>
        Answer
      </button>
    </div>
  );
}

function Approval({ card, assumptions, document, busy, onReply, onOpenDocument }: {
  card: ReadbackCard;
  assumptions: string[];
  document: Record<string, unknown>;
  busy: boolean;
  onReply: (payload: Record<string, unknown>) => void;
  onOpenDocument: (d: Record<string, unknown>) => void;
}) {
  const [mode, setMode] = useState<"view" | "change" | "edit">("view");
  const [change, setChange] = useState("");
  const [json, setJson] = useState(() => JSON.stringify(document, null, 2));
  const [jsonError, setJsonError] = useState<string | null>(null);
  const runs = sweepCount(document);

  const submitEdit = () => {
    try {
      onReply({ action: "edit", document: JSON.parse(json) });
    } catch (e) {
      setJsonError(`Not valid JSON: ${(e as Error).message}`);
    }
  };

  return (
    <div className="card readback">
      <div className="readback-title">Check before anything runs: {card.title}</div>
      {card.sections.map((s) => (
        <div key={s.heading} className="readback-section">
          <div className="readback-heading">{s.heading}</div>
          <ul>{s.lines.map((l, i) => <li key={i}>{l}</li>)}</ul>
        </div>
      ))}
      {assumptions.length > 0 && (
        <div className="readback-section assumptions">
          <div className="readback-heading">Assumed (not stated by you)</div>
          <ul>{assumptions.map((a, i) => <li key={i}>{a}</li>)}</ul>
        </div>
      )}

      {mode === "view" && (
        <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
          <button className="btn" disabled={busy} onClick={() => onReply({ action: "approve" })}>
            {runs > 0 ? `Approve and run ${runs} backtests` : "Approve and backtest"}
          </button>
          <button className="btn secondary" disabled={busy} onClick={() => setMode("change")}>Change something</button>
          <button className="btn secondary tiny" disabled={busy} onClick={() => setMode("edit")}>Edit JSON</button>
          <button className="btn secondary tiny" onClick={() => onOpenDocument(document)}>Open in builder</button>
          <button className="btn danger tiny" disabled={busy} onClick={() => onReply({ action: "reject" })}>Reject</button>
        </div>
      )}
      {mode === "change" && (
        <div>
          <textarea rows={2} value={change} aria-label="Requested change" placeholder="What should be different?"
            onChange={(e) => setChange(e.target.value)} />
          <div className="row" style={{ gap: 6 }}>
            <button className="btn" disabled={busy || !change.trim()} onClick={() => onReply({ action: "change", text: change.trim() })}>Send change</button>
            <button className="btn secondary tiny" onClick={() => setMode("view")}>Back</button>
          </div>
        </div>
      )}
      {mode === "edit" && (
        <div>
          <textarea className="mono" rows={14} value={json} aria-label="Strategy JSON" onChange={(e) => setJson(e.target.value)} />
          {jsonError && <div className="notice error">{jsonError}</div>}
          <div className="row" style={{ gap: 6 }}>
            <button className="btn" disabled={busy} onClick={submitEdit}>Use this version</button>
            <button className="btn secondary tiny" onClick={() => setMode("view")}>Back</button>
          </div>
        </div>
      )}
    </div>
  );
}
