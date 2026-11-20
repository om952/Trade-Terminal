// The screener: one condition tree, in the strategy language, run across many
// stocks on daily bars. The editor reuses the Builder's indicator and
// condition panels, so a scan reads like a strategy's entry; the server
// evaluates it with the backtest's own condition code.
//
// Results list every stock, including the ones that could not be scanned and
// why. Nothing is ranked by how good it looks: "now" orders by what just
// happened, and "history" sets every figure next to a buy-and-hold baseline.

import { useEffect, useMemo, useState } from "react";
import { api, ApiError, TERMINAL_JOB } from "./api";
import type {
  IndicatorMeta, Job, PrefetchPlan, Question, ScanCompileResult, PrefetchResult, ReturnStats, SavedScan, SavedScanDetail, ScanCheckResult, ScanResult, ScanRow, ScanStatus,
  ScanValidation,
} from "./api";
import { ConditionPanel, IndicatorPanel, PRICE_FIELDS } from "./Builder";

type Doc = Record<string, any>;

const SCAN_CONTEXT = ["prev_day_high", "prev_day_low", "prev_day_close", "pivot", "tc", "bc",
  "r1", "r2", "r3", "s1", "s2", "s3", "cpr_width"];

const POLL_MS = 1000;

export const STARTERS: { key: string; label: string; spec: Doc }[] = [
  {
    key: "breakout20",
    label: "Close above the 20-day high",
    spec: {
      name: "20-day breakout", side: "long", lookback: 20, indicators: [],
      condition: { op: "AND", conditions: [
        { left: "close", cmp: ">", right: { fn: "quantile", of: "high", window: 20, q: 1 } },
      ] },
    },
  },
  {
    key: "breakdown20",
    label: "Close below the 20-day low",
    spec: {
      name: "20-day breakdown", side: "short", lookback: 20, indicators: [],
      condition: { op: "AND", conditions: [
        { left: "close", cmp: "<", right: { fn: "quantile", of: "low", window: 20, q: 0 } },
      ] },
    },
  },
  {
    key: "above_sma50",
    label: "Close above the 50-day average",
    spec: {
      name: "Above 50-day SMA", side: "long", lookback: 20,
      indicators: [{ id: "sma50", type: "SMA", params: { period: 50, source: "close" } }],
      condition: { op: "AND", conditions: [{ left: "close", cmp: ">", right: "sma50" }] },
    },
  },
  {
    key: "rsi_oversold",
    label: "RSI(14) below 30",
    spec: {
      name: "RSI oversold", side: "long", lookback: 20,
      indicators: [{ id: "rsi", type: "RSI", params: { period: 14, source: "close" } }],
      condition: { op: "AND", conditions: [{ left: "rsi", cmp: "<", right: 30 }] },
    },
  },
];

export const BLANK_SCAN: Doc = { ...STARTERS[0].spec, universe: { preset: "fno_stocks" } };

/** Poll a job until it finishes. */
export function useJobPoll<R>(job: Job<R> | null, setJob: (j: Job<R>) => void, onError: (m: string) => void) {
  useEffect(() => {
    if (!job || TERMINAL_JOB.includes(job.status)) return;
    const timer = setTimeout(async () => {
      try { setJob(await api.job<R>(job.id)); } catch (e) { onError((e as ApiError).message); }
    }, POLL_MS);
    return () => clearTimeout(timer);
  }, [job]);
}

/* ------------------------------------------------------------------ */

export function ScreenerPanel({ spec, setSpec, catalogue, running, onRun, savedId, setSavedId }: {
  spec: Doc;
  setSpec: (d: Doc) => void;
  catalogue: IndicatorMeta[];
  running: boolean;
  /** Run a scan; `override` runs that spec instead of the one being edited. */
  onRun: (mode: "now" | "history", override?: Doc) => void;
  savedId: string | null;
  setSavedId: (id: string | null) => void;
}) {
  const [check, setCheck] = useState<ScanValidation | null>(null);
  const [symbolsText, setSymbolsText] = useState((spec.universe?.symbols ?? []).join(", "));
  const usePreset = Boolean(spec.universe?.preset);

  useEffect(() => {
    const timer = setTimeout(async () => {
      try { setCheck(await api.validateScan(spec)); } catch { setCheck(null); }
    }, 300);
    return () => clearTimeout(timer);
  }, [spec]);

  const options = useMemo(() => {
    const names = [...PRICE_FIELDS];
    for (const ind of spec.indicators ?? []) {
      const meta = catalogue.find((m) => m.name === ind.type);
      if (meta && meta.outputs.length > 1) names.push(...meta.outputs.map((o) => `${ind.id}.${o}`));
      else names.push(ind.id);
    }
    return [...names, ...SCAN_CONTEXT];
  }, [spec.indicators, catalogue]);

  const setUniverse = (preset: boolean, text = symbolsText) => {
    const symbols = text.split(/[\s,]+/).map((s: string) => s.trim()).filter(Boolean);
    setSpec({ ...spec, universe: preset ? { preset: "fno_stocks" } : { symbols } });
  };

  return (
    <>
      <ScanAssistant
        onUse={(d, run) => {
          setSpec(d);
          setSymbolsText(((d.universe as Doc | undefined)?.symbols ?? []).join(", "));
          setSavedId(null);
          if (run) onRun("now", d);      // this spec, not the one in the editor before the update
        }} />

      <SavedScans spec={spec} setSpec={setSpec} valid={Boolean(check?.valid)}
        savedId={savedId} setSavedId={setSavedId}
        onLoaded={(d) => setSymbolsText(((d.universe as Doc | undefined)?.symbols ?? []).join(", "))} />

      <div className="panel">
        <div className="panel-title">Start from</div>
        <select className="mono" value="" aria-label="Starter scan" onChange={(e) => {
          const starter = STARTERS.find((s) => s.key === e.target.value);
          if (starter) setSpec({ ...structuredClone(starter.spec), universe: spec.universe });
        }}>
          <option value="">— choose a starter scan —</option>
          {STARTERS.map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}
        </select>
        <div className="field" style={{ marginTop: 8 }}>
          <label htmlFor="scan-name">Name</label>
          <input id="scan-name" type="text" value={spec.name ?? ""}
            onChange={(e) => setSpec({ ...spec, name: e.target.value })} />
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
          <textarea rows={3} className="mono" aria-label="Symbols to scan" value={symbolsText}
            placeholder="RELIANCE, TCS, HDFCBANK"
            onChange={(e) => { setSymbolsText(e.target.value); setUniverse(false, e.target.value); }} />
        )}
        <div className="hint">
          {usePreset
            ? "Stocks with NSE futures, from today's instrument master. Past results miss stocks that left the list."
            : "Trading symbols or instrument keys, separated by commas."}
        </div>
        <HistoryPrefetch universe={spec.universe} enabled={Boolean(check?.universe)} />
      </div>

      <IndicatorPanel doc={spec} setDoc={setSpec} catalogue={catalogue} />
      <ConditionPanel title="Scan for" node={spec.condition} options={options}
        onChange={(v) => setSpec({ ...spec, condition: v ?? { op: "AND", conditions: [] } })} />

      <div className="panel">
        <div className="row">
          <div className="field">
            <label htmlFor="scan-side">Side (a label)</label>
            <select id="scan-side" className="mono" value={spec.side ?? ""}
              onChange={(e) => setSpec({ ...spec, side: e.target.value || null })}>
              <option value="">none</option><option value="long">long</option><option value="short">short</option>
            </select>
          </div>
          <div className="field">
            <label htmlFor="scan-lookback">Look back (days)</label>
            <input id="scan-lookback" type="number" min={1} max={250} className="mono" value={spec.lookback ?? 20}
              onChange={(e) => setSpec({ ...spec, lookback: Number(e.target.value) || 20 })} />
          </div>
        </div>
        <div className="hint">Daily bars. A stock that held the condition within the look-back shows when.</div>
      </div>

      {check && !check.valid && (
        <div className="notice error">
          <div className="notice-title">Cannot scan — fix these first</div>
          <ul>{check.errors.map((e, i) => <li key={i}>{e.field && <code>{e.field}</code>} {e.message}</li>)}</ul>
        </div>
      )}

      <div className="row" style={{ gap: 6 }}>
        <button className="btn" disabled={!check?.valid || running} onClick={() => onRun("now")}>
          {running ? <><span className="spinner" /> Scanning…</> : "Scan now"}
        </button>
        <button className="btn secondary" disabled={!check?.valid || running} onClick={() => onRun("history")}>
          Scan history
        </button>
      </div>
    </>
  );
}

/* ------------------------------------------------------------------ */

/** Describe a scan in words; read it back; use it only when the trader says so. */
export function ScanAssistant({ onUse }: {
  onUse: (spec: Doc, runNow: boolean) => void;
}) {
  const [text, setText] = useState("");
  const [job, setJob] = useState<Job<ScanCompileResult> | null>(null);
  const [answers, setAnswers] = useState<string[]>([]);
  const [changing, setChanging] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  useJobPoll(job, setJob, setProblem);
  const working = job != null && !TERMINAL_JOB.includes(job.status);
  const result = job?.status === "succeeded" ? job.result : null;

  useEffect(() => { setAnswers((result?.questions ?? []).map(() => "")); }, [result]);

  const send = async (payload: { text: string; dialogue?: ScanCompileResult["dialogue"]; current?: Doc }) => {
    setProblem(null);
    try { setJob(await api.compileScan(payload)); } catch (e) { setProblem((e as ApiError).message); }
  };

  const answerText = (questions: Question[]) =>
    questions.map((q, i) => `Q: ${q.question}\nA: ${answers[i]}`).join("\n");

  return (
    <div className="panel">
      <div className="panel-title"><span>Describe a scan</span></div>
      <textarea rows={3} aria-label="Describe a scan" value={text}
        placeholder="e.g. F&O stocks that closed above their 20-day high with volume above its 20-day average"
        onChange={(e) => setText(e.target.value)} />
      <button className="btn" style={{ width: "100%" }} disabled={working || !text.trim()}
        onClick={() => { setChanging(false); send({ text: text.trim() }); }}>
        {working ? <><span className="spinner" /> {job?.message || "Reading the screen…"}</> : "Build scan"}
      </button>
      {problem && <div className="notice error">{problem}</div>}
      {job?.status === "failed" && <div className="notice error">{job.error}</div>}

      {result?.status === "needs_clarification" && result.questions && (
        <div className="card" style={{ marginTop: 8 }}>
          {result.questions.map((q, i) => (
            <div key={i} className="field">
              <label htmlFor={`scan-q-${i}`}>{q.question}</label>
              <span className="hint">{q.why}</span>
              <div className="chip-row">
                {q.options.map((o) => (
                  <button key={o} className={`chip ${answers[i] === o ? "active" : ""}`}
                    onClick={() => setAnswers(answers.map((a, j) => (j === i ? o : a)))}>{o}</button>
                ))}
              </div>
              <input id={`scan-q-${i}`} type="text" value={answers[i] ?? ""} placeholder="or type an answer"
                onChange={(e) => setAnswers(answers.map((a, j) => (j === i ? e.target.value : a)))} />
            </div>
          ))}
          <button className="btn" disabled={working || answers.some((a) => !a?.trim())}
            onClick={() => send({ text: answerText(result.questions!), dialogue: result.dialogue })}>Answer</button>
        </div>
      )}

      {result?.status === "ready" && result.card && result.spec && (
        <div className="card readback" style={{ marginTop: 8 }}>
          <div className="readback-title">Check before scanning: {result.card.title}</div>
          {result.change_summary && <div className="hint">Change: {result.change_summary}</div>}
          {result.card.sections.map((sec) => (
            <div key={sec.heading} className="readback-section">
              <div className="readback-heading">{sec.heading}</div>
              <ul>{sec.lines.map((l, i) => <li key={i}>{l}</li>)}</ul>
            </div>
          ))}
          {(result.assumptions ?? []).length > 0 && (
            <div className="readback-section assumptions">
              <div className="readback-heading">Assumed (not stated by you)</div>
              <ul>{result.assumptions!.map((a, i) => <li key={i}>{a}</li>)}</ul>
            </div>
          )}
          {!changing ? (
            <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
              <button className="btn" onClick={() => onUse(result.spec as Doc, true)}>Use and scan now</button>
              <button className="btn secondary" onClick={() => onUse(result.spec as Doc, false)}>Use this scan</button>
              <button className="btn secondary tiny" onClick={() => { setChanging(true); setText(""); }}>Change…</button>
            </div>
          ) : (
            <div>
              <textarea rows={2} aria-label="Requested change" value={text} placeholder="What should be different?"
                onChange={(e) => setText(e.target.value)} />
              <div className="row" style={{ gap: 6 }}>
                <button className="btn" disabled={working || !text.trim()}
                  onClick={() => send({ text: text.trim(), current: result.spec as Doc })}>Send change</button>
                <button className="btn secondary tiny" onClick={() => setChanging(false)}>Back</button>
              </div>
            </div>
          )}
        </div>
      )}

      {result && ["unsupported", "failed"].includes(result.status) && (
        <div className="notice error">{result.reason ?? result.error}</div>
      )}
      {!result && !working && <div className="hint">Nothing runs until you choose to use the scan.</div>}
    </div>
  );
}

/* ------------------------------------------------------------------ */

function duration(seconds: number) {
  if (seconds < 90) return `${Math.round(seconds)}s`;
  const minutes = Math.round(seconds / 60);
  return minutes < 90 ? `${minutes} min` : `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`;
}

/** Daily history for the universe: what is missing, and a resumable download within the rate limits. */
export function HistoryPrefetch({ universe, enabled }: { universe: unknown; enabled: boolean }) {
  const [plan, setPlan] = useState<PrefetchPlan | null>(null);
  const [job, setJob] = useState<Job<PrefetchResult> | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  useJobPoll(job, setJob, setProblem);
  const running = job != null && !TERMINAL_JOB.includes(job.status);
  const stopped = job != null && ["cancelled", "interrupted", "failed"].includes(job.status);

  const check = async () => {
    setProblem(null);
    try { setPlan(await api.prefetchPlan(universe)); } catch (e) { setProblem((e as ApiError).message); }
  };
  useEffect(() => { setPlan(null); }, [JSON.stringify(universe)]);
  useEffect(() => { if (job && TERMINAL_JOB.includes(job.status)) check(); }, [job?.status]);

  const start = async () => {
    setProblem(null);
    try {
      setJob(stopped ? await api.resumeJob<PrefetchResult>(job!.id) : await api.prefetch(universe));
    } catch (e) { setProblem((e as ApiError).message); }
  };

  return (
    <div style={{ marginTop: 8 }}>
      <div className="row" style={{ gap: 6, alignItems: "center" }}>
        <button className="btn secondary tiny" style={{ flex: "0 0 auto" }} disabled={!enabled || running} onClick={check}>
          Check history
        </button>
        {plan && plan.calls > 0 && (
          <button className="btn tiny" style={{ flex: "0 0 auto" }} disabled={!plan.broker_ready || running} onClick={start}>
            {stopped ? "Resume download" : "Download"}
          </button>
        )}
        {running && <button className="btn secondary tiny" style={{ flex: "0 0 auto" }}
          onClick={async () => { try { setJob(await api.cancelJob(job!.id) as Job<PrefetchResult>); } catch { /* finished */ } }}>Cancel</button>}
      </div>
      {plan && (
        <div className="hint" role="status" aria-label="History plan">
          Daily bars {plan.start} to {plan.end}: {plan.up_to_date} of {plan.instruments} up to date
          {plan.skipped ? `, ${plan.skipped} not downloadable` : ""}.
          {plan.calls > 0 && <> {plan.to_download} need {plan.missing_sessions.toLocaleString()} sessions in {plan.calls.toLocaleString()} request{plan.calls === 1 ? "" : "s"}, about {duration(plan.eta_seconds)}{plan.binding_limit ? ` (Upstox allows ${plan.binding_limit})` : ""}.</>}
          {plan.calls > 0 && !plan.broker_ready && <> Upstox is not connected: {plan.broker_reason}</>}
        </div>
      )}
      {running && (
        <div role="status">
          <div className="hint">{job.message || "Starting the download…"}</div>
          <div className="progress"><div style={{ width: `${Math.round(job.progress * 100)}%` }} /></div>
        </div>
      )}
      {stopped && <div className="hint">Download stopped{job.error ? `: ${job.error}` : ""}. Resuming skips what is already stored.</div>}
      {job?.status === "succeeded" && job.result && (
        <div className="hint">
          Downloaded {job.result.sessions_fetched.toLocaleString()} sessions in {duration(job.result.elapsed_seconds)}
          {job.result.errors.length ? `; ${job.result.errors.length} failed: ${job.result.errors.map((e) => e.symbol).join(", ")}` : ""}.
        </div>
      )}
      {problem && <div className="hint neg">{problem}</div>}
    </div>
  );
}

/* ------------------------------------------------------------------ */

/** Saved scans: load, save versions, daily alerts on changes, and their history. */
export function SavedScans({ spec, setSpec, valid, savedId, setSavedId, onLoaded }: {
  spec: Doc;
  setSpec: (d: Doc) => void;
  valid: boolean;
  savedId: string | null;
  setSavedId: (id: string | null) => void;
  onLoaded?: (d: Doc) => void;
}) {
  const [list, setList] = useState<SavedScan[]>([]);
  const [meta, setMeta] = useState<{ alert_times: string; telegram: boolean } | null>(null);
  const [detail, setDetail] = useState<SavedScanDetail | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [checkJob, setCheckJob] = useState<Job<ScanCheckResult> | null>(null);
  useJobPoll(checkJob, setCheckJob, (m) => setNote(m));

  const refresh = async (id = savedId) => {
    try {
      const all = await api.savedScans();
      setList(all.scans);
      setMeta({ alert_times: all.alert_times, telegram: all.telegram });
      setDetail(id ? await api.savedScan(id) : null);
    } catch { /* backend down */ }
  };
  useEffect(() => { refresh(); }, [savedId]);
  useEffect(() => { if (checkJob && TERMINAL_JOB.includes(checkJob.status)) refresh(); }, [checkJob?.status]);

  const load = async (id: string) => {
    setNote(null);
    if (!id) { setSavedId(null); return; }
    const scan = await api.savedScan(id);
    setSpec(scan.document);
    onLoaded?.(scan.document);
    setSavedId(id);
  };

  const save = async (alerts?: boolean) => {
    setNote(null);
    try {
      const out = await api.saveScan(spec, savedId ?? undefined, alerts);
      setSavedId(out.id);
      setNote(alerts !== undefined
        ? (alerts ? "Daily alert on" : "Daily alert off")
        : savedId ? (out.new_version ? `Saved as version ${out.version}` : "No changes to save") : "Saved");
      refresh(out.id);
    } catch (e) {
      setNote(`Not saved: ${(e as ApiError).message}`);
      refresh();
    }
  };

  const checkResult = checkJob?.status === "succeeded" ? checkJob.result : null;

  return (
    <div className="panel">
      <div className="panel-title"><span>Saved scans</span></div>
      <div className="row" style={{ gap: 6 }}>
        <select className="mono" aria-label="Saved scan" value={savedId ?? ""} onChange={(e) => load(e.target.value)}>
          <option value="">— unsaved scan —</option>
          {list.map((s) => <option key={s.id} value={s.id}>{s.name}{s.alerts ? " · alert" : ""}</option>)}
        </select>
        <button className="btn secondary tiny" style={{ flex: "0 0 auto" }} disabled={!valid} onClick={() => save()}>
          {savedId ? "Save version" : "Save"}
        </button>
      </div>
      {note && <div className="hint">{note}</div>}

      {detail && (
        <>
          <label className="row" style={{ alignItems: "center", gap: 6, marginTop: 8 }}>
            <input type="checkbox" style={{ flex: "0 0 auto", width: "auto" }} checked={detail.alerts}
              disabled={!valid} aria-label="Daily alert"
              onChange={(e) => { setDetail({ ...detail, alerts: e.target.checked }); save(e.target.checked); }} />
            <span>Alert me when a stock triggers or falls back inside</span>
          </label>
          <div className="hint">
            Checked at {meta?.alert_times ?? "08:45"} IST against the last completed session.{" "}
            {meta?.telegram ? "Sent to Telegram." : "Telegram is not set up, so changes show here only."}
            {detail.last_checked_at ? ` Last scheduled check ${detail.last_checked_at.slice(0, 16).replace("T", " ")}.` : ""}
          </div>
          <div className="row" style={{ gap: 6, marginTop: 6 }}>
            <button className="btn secondary tiny" style={{ flex: "0 0 auto" }}
              disabled={checkJob != null && !TERMINAL_JOB.includes(checkJob.status)}
              onClick={async () => { try { setCheckJob(await api.checkScan(detail.id)); } catch (e) { setNote((e as ApiError).message); } }}>
              Check now
            </button>
            <button className="btn danger tiny" style={{ flex: "0 0 auto" }}
              onClick={async () => { await api.archiveScan(detail.id); setSavedId(null); refresh(null); }}>
              Archive
            </button>
          </div>
          {checkResult && (
            <div className="hint" role="status">
              {checkResult.new_session === false
                ? `No new session since the last check (latest bar ${checkResult.as_of ?? "–"}).`
                : checkResult.baseline
                  ? `Baseline recorded: ${checkResult.active} of ${checkResult.scanned} meet the condition. Changes are reported from the next session.`
                  : `${checkResult.events.length} change${checkResult.events.length === 1 ? "" : "s"} at the close of ${checkResult.as_of}.`}
            </div>
          )}
          {checkJob?.status === "failed" && <div className="notice error">{checkJob.error}</div>}

          <div className="readback-heading" style={{ marginTop: 8 }}>Recent changes</div>
          {detail.events.length === 0
            ? <div className="hint">None yet.</div>
            : (
              <ul className="caveats" aria-label="Recent changes">
                {detail.events.slice(0, 15).map((e) => (
                  <li key={e.id}>
                    <span className="mono">{e.as_of}</span> <b>{e.symbol}</b>{" "}
                    <span className={e.kind === "triggered" ? "pos" : ""}>
                      {e.kind === "triggered" ? "triggered" : "back inside"}
                    </span>
                    {e.detail.distance_pct != null ? ` (${e.detail.distance_pct > 0 ? "+" : ""}${e.detail.distance_pct.toFixed(1)}%)` : ""}
                  </li>
                ))}
              </ul>
            )}
        </>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */

const STATUS_LABEL: Record<ScanStatus, string> = {
  triggered_today: "Triggered today",
  holding: "Holding",
  lapsed: "Lapsed",
  waiting: "Waiting",
  not_enough_data: "Too little history",
  no_data: "No data",
  error: "Error",
  ok: "Scanned",
};

const STATUS_HINT: Partial<Record<ScanStatus, string>> = {
  holding: "True now, and since an earlier day",
  lapsed: "Held within the look-back, not true on the latest bar",
  waiting: "Not true anywhere in the look-back",
};

const UNSCANNED: ScanStatus[] = ["not_enough_data", "no_data", "error"];

function fmt(n: number | null | undefined, digits = 2) {
  return n == null ? "–" : n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function distance(row: ScanRow) {
  const d = row.distance_pct;
  if (d == null) return "–";
  return d >= 0 ? `+${fmt(d, 1)}% beyond` : `${fmt(-d, 1)}% to go`;
}

function ago(bars: number | null | undefined) {
  if (bars == null) return "";
  return bars === 0 ? "latest bar" : `${bars} bar${bars === 1 ? "" : "s"} ago`;
}

export function ScanResults({ job, result, onCancel }: {
  job: Job<ScanResult> | null;
  result: ScanResult | null;
  onCancel: () => void;
}) {
  const running = job != null && !TERMINAL_JOB.includes(job.status);
  return (
    <>
      {running && (
        <div className="panel" role="status">
          <div className="row" style={{ justifyContent: "space-between" }}>
            <span><span className="spinner" /> {job.message || "Starting the scan…"}</span>
            <button className="btn secondary tiny" style={{ flex: "0 0 auto" }} onClick={onCancel}>Cancel</button>
          </div>
          <div className="progress"><div style={{ width: `${Math.round(job.progress * 100)}%` }} /></div>
        </div>
      )}
      {job?.status === "cancelled" && <div className="notice warn">Scan cancelled.</div>}
      {job?.status === "failed" && <div className="notice error"><div className="notice-title">Scan failed</div>{job.error}</div>}
      {result && (result.mode === "now" ? <NowTable result={result} /> : <HistoryView result={result} />)}
    </>
  );
}

function Header({ result }: { result: ScanResult }) {
  return (
    <>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "baseline", marginBottom: 6 }}>
        <b style={{ flex: "1 1 auto" }}>
          {result.name} · {result.mode === "now" ? `as of ${result.as_of ?? "–"}` : "history"} · {result.universe} stocks
        </b>
      </div>
      <div className="chip-row" style={{ marginBottom: 8 }}>
        {Object.entries(result.counts).map(([status, n]) => (
          <span key={status} className="status-chip">{STATUS_LABEL[status as ScanStatus] ?? status} <b>{n}</b></span>
        ))}
      </div>
      {result.notes.length > 0 && <ul className="caveats">{result.notes.map((n, i) => <li key={i}>{n}</li>)}</ul>}
    </>
  );
}

function NowTable({ result }: { result: ScanResult }) {
  const scanned = result.rows.filter((r) => !UNSCANNED.includes(r.status));
  const skipped = result.rows.filter((r) => UNSCANNED.includes(r.status));
  return (
    <div>
      <Header result={result} />
      <div className="table-wrap">
        <table style={{ minWidth: 0 }}>
          <thead>
            <tr><th>Stock</th>{result.side && <th>Side</th>}<th>Status</th><th>Close</th><th>Triggered on</th><th>Distance</th></tr>
          </thead>
          <tbody>
            {scanned.map((r) => {
              const when = r.status === "lapsed" ? r.last_fired : r.since;
              const then = r.status === "lapsed" ? r.last_fired_close : r.since_close;
              const bars = r.status === "lapsed" ? r.bars_since : null;
              return (
                <tr key={r.key || r.symbol}>
                  <td>
                    <b>{r.symbol}</b>
                    {r.data?.synthetic && <span className="tag warn" style={{ marginLeft: 6 }}>synthetic</span>}
                    {r.stale && <span className="tag warn" style={{ marginLeft: 6 }} title={`Last bar ${r.as_of}`}>stale</span>}
                  </td>
                  {result.side && <td className={result.side === "long" ? "pos" : "neg"}>{result.side === "long" ? "▲ Long" : "▼ Short"}</td>}
                  <td title={STATUS_HINT[r.status]}>
                    <span className={r.status === "triggered_today" || r.status === "holding" ? "pos" : ""}>{STATUS_LABEL[r.status]}</span>
                  </td>
                  <td className="mono">{fmt(r.close)}</td>
                  <td>
                    {when ? <>{when}{r.since_capped ? " or earlier" : ""}<div className="hint">{[ago(bars), then != null ? `closed ${fmt(then)}` : ""].filter(Boolean).join(" · ")}</div></> : "–"}
                  </td>
                  <td className="mono">{distance(r)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {skipped.length > 0 && (
        <details style={{ marginTop: 8 }} open={scanned.length === 0}>
          <summary className="hint">{skipped.length} not scanned</summary>
          <ul className="caveats">
            {skipped.map((r) => <li key={r.key || r.symbol}><b>{r.symbol}</b>: {STATUS_LABEL[r.status]}{r.reason ? ` — ${r.reason}` : ""}</li>)}
          </ul>
        </details>
      )}
    </div>
  );
}

function Stats({ s, base }: { s: ReturnStats; base: ReturnStats }) {
  const cell = (v: number | null, b: number | null, suffix: string) => (
    <td className="mono">{fmt(v)}{suffix}<div className="hint">baseline {fmt(b)}{suffix}</div></td>
  );
  return (
    <>
      <td className="mono">{s.count}<div className="hint">of {base.count} bars</div></td>
      {cell(s.mean, base.mean, "%")}
      {cell(s.median, base.median, "%")}
      {cell(s.hit_rate, base.hit_rate, "%")}
    </>
  );
}

function HistoryView({ result }: { result: ScanResult }) {
  const events = result.events ?? [];
  const shown = events.slice(-300).reverse();
  return (
    <div>
      <Header result={result} />
      <div className="table-wrap" style={{ marginBottom: 10 }}>
        <table style={{ minWidth: 0 }}>
          <thead><tr><th>After</th><th>Events</th><th>Mean return</th><th>Median</th><th>Up share</th></tr></thead>
          <tbody>
            {Object.entries(result.summary ?? {}).map(([h, v]) => (
              <tr key={h}><td>{h} day{h === "1" ? "" : "s"}</td><Stats s={v.signal} base={v.baseline} /></tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="table-wrap table-scroll">
        <table style={{ minWidth: 0 }}>
          <thead><tr><th>Date</th><th>Stock</th><th>Close</th><th>+1 day</th><th>+5 days</th><th>+20 days</th></tr></thead>
          <tbody>
            {shown.map((e, i) => (
              <tr key={i}>
                <td className="mono">{e.date}</td><td>{e.symbol}</td><td className="mono">{fmt(e.close)}</td>
                {["1", "5", "20"].map((h) => {
                  const v = e.forward[h];
                  return <td key={h} className={`mono ${v == null ? "" : v > 0 ? "pos" : v < 0 ? "neg" : ""}`}>{v == null ? "–" : `${fmt(v)}%`}</td>;
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {events.length > shown.length && <p className="hint">Showing the latest {shown.length} of {events.length} events.</p>}
    </div>
  );
}
