// The visual strategy builder. Edits a strategy document held in App state.
//
// The document IS the strategy — this component never computes anything about
// trading, it only edits JSON that the backend validates and executes. That is
// why the live JSON preview is always visible: what you see is exactly what runs.

import { useEffect, useMemo, useState } from "react";
import type { Dataset, IndicatorMeta, InstrumentHit, TemplateMeta } from "./api";
import { api } from "./api";

type Doc = Record<string, any>;

const COMPARATORS = [
  { value: ">", label: "greater than" },
  { value: "<", label: "less than" },
  { value: ">=", label: "at or above" },
  { value: "<=", label: "at or below" },
  { value: "==", label: "equals" },
  { value: "crosses_above", label: "crosses above" },
  { value: "crosses_below", label: "crosses below" },
];

const PRICE_FIELDS = ["close", "open", "high", "low", "volume", "hl2", "hlc3", "ohlc4"];
const CONTEXT_FIELDS = ["pivot", "tc", "bc", "r1", "r2", "r3", "s1", "s2", "s3", "cpr_width",
  "prev_day_high", "prev_day_low", "prev_day_close", "vwap"];

const TIMEFRAMES = ["1m", "3m", "5m", "15m", "30m", "1h", "1d"];

interface Props {
  doc: Doc;
  setDoc: (doc: Doc) => void;
  indicators: IndicatorMeta[];
  templates: TemplateMeta[];
  datasets: Dataset[];
  validation: { valid: boolean; errors?: { field: string; message: string }[]; summary?: string; hash?: string } | null;
  onRun: () => void;
  running: boolean;
  runConfig: RunConfig;
  setRunConfig: (c: RunConfig) => void;
  onRefreshDatasets: () => void;
}

export interface RunConfig {
  symbol: string;
  starting_capital: number;
  lot_size: number;
  slippage_bps: number;
  max_leverage: number;
}

export function Builder(props: Props) {
  const { doc, setDoc, indicators, templates, datasets, validation } = props;

  const update = (path: string[], value: unknown) => {
    const next = structuredClone(doc);
    let node: any = next;
    for (const key of path.slice(0, -1)) node = node[key];
    node[path[path.length - 1]] = value;
    setDoc(next);
  };

  // Every name a condition may legally reference. Built from the document so a
  // freshly added indicator is immediately selectable.
  const referenceOptions = useMemo(() => {
    const names: string[] = [...PRICE_FIELDS];
    for (const ind of doc.indicators ?? []) {
      const meta = indicators.find((i) => i.name === ind.type);
      if (meta && meta.outputs.length > 1) {
        for (const out of meta.outputs) names.push(`${ind.id}.${out}`);
      } else {
        names.push(ind.id);
      }
    }
    names.push(...CONTEXT_FIELDS);
    return names;
  }, [doc.indicators, indicators]);

  return (
    <>
      <TemplatePicker templates={templates} onLoad={(d) => setDoc(d)} />
      <InstrumentPanel
        doc={doc}
        update={update}
        datasets={datasets}
        runConfig={props.runConfig}
        setRunConfig={props.setRunConfig}
        onRefreshDatasets={props.onRefreshDatasets}
      />
      <IndicatorPanel doc={doc} setDoc={setDoc} catalogue={indicators} />
      <ConditionPanel
        title="Entry when"
        node={doc.entry}
        options={referenceOptions}
        onChange={(v) => update(["entry"], v)}
      />
      <ConditionPanel
        title="Exit when"
        node={doc.exit}
        options={referenceOptions}
        optional
        onChange={(v) => update(["exit"], v)}
      />
      <LegPanel doc={doc} setDoc={setDoc} />
      <RiskPanel doc={doc} update={update} runConfig={props.runConfig} setRunConfig={props.setRunConfig} />

      {validation && !validation.valid && (
        <div className="notice error">
          <div className="notice-title">Cannot run — fix these first</div>
          <ul>
            {validation.errors?.map((e, i) => (
              <li key={i}>
                {e.field && <code>{e.field}</code>} {e.message}
              </li>
            ))}
          </ul>
        </div>
      )}

      <button
        className="btn"
        disabled={!validation?.valid || props.running}
        onClick={props.onRun}
        style={{ width: "100%", padding: "10px" }}
      >
        {props.running ? <><span className="spinner" /> Running…</> : "Run backtest"}
      </button>

      <div className="panel">
        <div className="panel-title">
          <span>Strategy document</span>
          {validation?.hash && <span style={{ fontSize: 9 }}>{validation.hash.slice(0, 10)}</span>}
        </div>
        <div className="json-preview">{JSON.stringify(doc, null, 2)}</div>
      </div>
    </>
  );
}

/* ------------------------------------------------------------------ */

function TemplatePicker({ templates, onLoad }: { templates: TemplateMeta[]; onLoad: (d: Doc) => void }) {
  const [busy, setBusy] = useState(false);

  const load = async (key: string) => {
    if (!key) return;
    setBusy(true);
    try {
      onLoad(await api.template(key));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <div className="panel-title">Start from a template</div>
      <select className="mono" disabled={busy} defaultValue="" onChange={(e) => load(e.target.value)}>
        <option value="">— blank / choose a template —</option>
        <optgroup label="Directional">
          {templates.filter((t) => t.category === "directional").map((t) => (
            <option key={t.key} value={t.key}>{t.name}</option>
          ))}
        </optgroup>
        <optgroup label="Options">
          {templates.filter((t) => t.category === "options").map((t) => (
            <option key={t.key} value={t.key}>{t.name}</option>
          ))}
        </optgroup>
      </select>
      <div className="hint" style={{ marginTop: 6 }}>
        Templates are ordinary documents. Load one, change anything, save it as your own.
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */

function InstrumentPanel({
  doc, update, datasets, runConfig, setRunConfig, onRefreshDatasets,
}: {
  doc: Doc; update: (p: string[], v: unknown) => void; datasets: Dataset[];
  runConfig: RunConfig; setRunConfig: (c: RunConfig) => void; onRefreshDatasets: () => void;
}) {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<InstrumentHit[]>([]);
  const [fetching, setFetching] = useState(false);

  useEffect(() => {
    if (query.trim().length < 2) { setHits([]); return; }
    const timer = setTimeout(async () => {
      try { setHits((await api.searchInstruments(query)).results); } catch { setHits([]); }
    }, 220);
    return () => clearTimeout(timer);
  }, [query]);

  const pick = (key: string) => {
    update(["instrument", "symbol"], key);
    setRunConfig({ ...runConfig, symbol: key });
    setQuery("");
    setHits([]);
  };

  const available = datasets.filter((d) => d.timeframe === doc.timeframe);

  const downloadData = async () => {
    setFetching(true);
    try {
      await api.fetchData({ symbol: runConfig.symbol, timeframe: doc.timeframe, days: 365 });
      onRefreshDatasets();
    } catch (e) {
      alert(`Fetch failed: ${(e as Error).message}`);
    } finally {
      setFetching(false);
    }
  };

  return (
    <div className="panel">
      <div className="panel-title">Instrument</div>

      <div className="field">
        <label>Stored datasets — these are what you can backtest</label>
        <select
          className="mono"
          value={runConfig.symbol}
          onChange={(e) => {
            update(["instrument", "symbol"], e.target.value);
            setRunConfig({ ...runConfig, symbol: e.target.value });
          }}
        >
          <option value="">— select —</option>
          {available.map((d) => (
            <option key={`${d.instrument_key}:${d.timeframe}`} value={d.instrument_key}>
              {d.instrument_key} · {d.bars.toLocaleString()} bars{d.synthetic ? " · SYNTHETIC" : ""}
            </option>
          ))}
        </select>
        {available.length === 0 && (
          <span className="hint">No data stored at {doc.timeframe}. Search below and download it.</span>
        )}
      </div>

      <div className="field">
        <label>Search all instruments</label>
        <input
          type="text"
          placeholder="RELIANCE, NIFTY, GOLD…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        {hits.length > 0 && (
          <div className="search-results">
            {hits.map((h) => (
              <div className="search-item" key={h.instrument_key} onClick={() => pick(h.instrument_key)}>
                <span className="sym">{h.trading_symbol}</span>
                <span className="meta">{h.exchange} · {h.instrument_type} · lot {h.lot_size}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="row">
        <div className="field">
          <label>Timeframe</label>
          <select className="mono" value={doc.timeframe} onChange={(e) => update(["timeframe"], e.target.value)}>
            {TIMEFRAMES.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </div>
        <div className="field">
          <label>Segment</label>
          <select
            className="mono"
            value={doc.instrument?.segment ?? "index"}
            onChange={(e) => update(["instrument", "segment"], e.target.value)}
          >
            {["index", "equity", "future", "option", "commodity"].map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </div>
      </div>

      <button className="btn secondary tiny" onClick={downloadData} disabled={fetching || !runConfig.symbol}>
        {fetching ? <><span className="spinner" /> Downloading…</> : "Download 1y of history from Upstox"}
      </button>
    </div>
  );
}

/* ------------------------------------------------------------------ */

function IndicatorPanel({ doc, setDoc, catalogue }: { doc: Doc; setDoc: (d: Doc) => void; catalogue: IndicatorMeta[] }) {
  const [adding, setAdding] = useState("");

  const byCategory = useMemo(() => {
    const groups: Record<string, IndicatorMeta[]> = {};
    for (const meta of catalogue) (groups[meta.category] ??= []).push(meta);
    return groups;
  }, [catalogue]);

  const add = (type: string) => {
    if (!type) return;
    const meta = catalogue.find((i) => i.name === type);
    if (!meta) return;

    // Auto-name so ids stay unique without the user having to think about it.
    const base = type.toLowerCase();
    const existing = (doc.indicators ?? []).map((i: any) => i.id);
    let id = base, n = 2;
    while (existing.includes(id)) id = `${base}_${n++}`;

    setDoc({ ...doc, indicators: [...(doc.indicators ?? []), { id, type, params: { ...meta.params } }] });
    setAdding("");
  };

  const remove = (index: number) => {
    const next = [...(doc.indicators ?? [])];
    next.splice(index, 1);
    setDoc({ ...doc, indicators: next });
  };

  const setParam = (index: number, key: string, raw: string) => {
    const next = structuredClone(doc.indicators ?? []);
    const asNumber = Number(raw);
    next[index].params[key] = raw !== "" && !Number.isNaN(asNumber) ? asNumber : raw;
    setDoc({ ...doc, indicators: next });
  };

  const rename = (index: number, id: string) => {
    const next = structuredClone(doc.indicators ?? []);
    next[index].id = id;
    setDoc({ ...doc, indicators: next });
  };

  return (
    <div className="panel">
      <div className="panel-title">Indicators</div>

      {(doc.indicators ?? []).map((ind: any, i: number) => (
        <div className="rule" key={i}>
          <div className="rule-head">
            <input
              className="mono"
              style={{ width: 118 }}
              value={ind.id}
              onChange={(e) => rename(i, e.target.value)}
            />
            <span className="rule-label">{ind.type}</span>
            <button className="remove" onClick={() => remove(i)} title="Remove">×</button>
          </div>
          <div className="row">
            {Object.entries(ind.params ?? {}).map(([key, value]) => (
              <div className="field" key={key} style={{ marginBottom: 0 }}>
                <label>{key}</label>
                <input
                  className="mono"
                  value={String(value)}
                  onChange={(e) => setParam(i, key, e.target.value)}
                />
              </div>
            ))}
          </div>
        </div>
      ))}

      <select className="mono" value={adding} onChange={(e) => add(e.target.value)}>
        <option value="">+ add indicator…</option>
        {Object.entries(byCategory).map(([category, items]) => (
          <optgroup key={category} label={category}>
            {items.map((m) => <option key={m.name} value={m.name}>{m.name} — {m.description}</option>)}
          </optgroup>
        ))}
      </select>
    </div>
  );
}

/* ------------------------------------------------------------------ */

function ConditionPanel({
  title, node, options, onChange, optional,
}: {
  title: string;
  node: Doc | null | undefined;
  options: string[];
  onChange: (v: Doc | null) => void;
  optional?: boolean;
}) {
  const conditions = node?.conditions ?? [];

  const setCondition = (index: number, patch: Doc) => {
    const next = structuredClone(node ?? { op: "AND", conditions: [] });
    next.conditions[index] = { ...next.conditions[index], ...patch };
    onChange(next);
  };

  const add = () => {
    const next = structuredClone(node ?? { op: "AND", conditions: [] });
    next.conditions.push({ left: "close", cmp: ">", right: 0 });
    onChange(next);
  };

  const remove = (index: number) => {
    if (!node) return;
    const next = structuredClone(node);
    next.conditions.splice(index, 1);
    onChange(next.conditions.length ? next : optional ? null : { op: "AND", conditions: [] });
  };

  // A right-hand operand is either a reference name or a raw number. Numbers are
  // stored as numbers, not strings, so the schema validates them as literals.
  const operandValue = (v: unknown) => (typeof v === "number" ? String(v) : String(v ?? ""));
  const parseOperand = (raw: string): string | number => {
    const n = Number(raw);
    return raw.trim() !== "" && !Number.isNaN(n) ? n : raw;
  };

  return (
    <div className="panel">
      <div className="panel-title">
        <span>{title}</span>
        {conditions.length > 1 && (
          <div className="chip-row">
            {["AND", "OR"].map((op) => (
              <button
                key={op}
                className={`chip ${node?.op === op ? "active" : ""}`}
                onClick={() => onChange({ ...structuredClone(node), op })}
              >
                {op}
              </button>
            ))}
          </div>
        )}
      </div>

      {conditions.map((c: any, i: number) => (
        <div className="rule" key={i}>
          <div className="rule-head">
            <span className="rule-label">{i === 0 ? "if" : String(node?.op ?? "AND").toLowerCase()}</span>
            <button className="remove" onClick={() => remove(i)}>×</button>
          </div>

          <div className="field" style={{ marginBottom: 6 }}>
            <select className="mono" value={String(c.left)} onChange={(e) => setCondition(i, { left: e.target.value })}>
              {options.map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
          </div>

          <div className="row">
            <select className="mono" value={c.cmp} onChange={(e) => setCondition(i, { cmp: e.target.value })}>
              {COMPARATORS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
            <input
              className="mono"
              list={`refs-${title.replace(/\s/g, "")}`}
              value={operandValue(c.right)}
              onChange={(e) => setCondition(i, { right: parseOperand(e.target.value) })}
              placeholder="value or indicator"
            />
          </div>
        </div>
      ))}

      <datalist id={`refs-${title.replace(/\s/g, "")}`}>
        {options.map((o) => <option key={o} value={o} />)}
      </datalist>

      <button className="btn secondary tiny" onClick={add}>+ condition</button>
      {optional && conditions.length === 0 && (
        <div className="hint" style={{ marginTop: 6 }}>
          No exit conditions — the position closes on stop, target or square-off.
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */

function LegPanel({ doc, setDoc }: { doc: Doc; setDoc: (d: Doc) => void }) {
  const legs = doc.legs ?? [];

  const patch = (index: number, changes: Doc) => {
    const next = structuredClone(legs);
    next[index] = { ...next[index], ...changes };
    setDoc({ ...doc, legs: next });
  };

  const setExit = (index: number, field: "stop_loss" | "target" | "trail", changes: Doc | null) => {
    const next = structuredClone(legs);
    if (changes === null) delete next[index][field];
    else next[index][field] = { ...(next[index][field] ?? { type: "percent", value: 1 }), ...changes };
    setDoc({ ...doc, legs: next });
  };

  const add = () => {
    const ids = legs.map((l: any) => l.id);
    let id = "leg1", n = 1;
    while (ids.includes(id)) id = `leg${++n}`;
    setDoc({ ...doc, legs: [...legs, { id, action: "BUY", kind: "equity", lots: 1 }] });
  };

  const atrIds = (doc.indicators ?? []).filter((i: any) => i.type === "ATR").map((i: any) => i.id);

  return (
    <div className="panel">
      <div className="panel-title">Legs</div>

      {legs.map((leg: any, i: number) => (
        <div className="rule" key={i}>
          <div className="rule-head">
            <span className="rule-label">{leg.id}</span>
            {legs.length > 1 && (
              <button className="remove" onClick={() => {
                const next = [...legs]; next.splice(i, 1); setDoc({ ...doc, legs: next });
              }}>×</button>
            )}
          </div>

          <div className="row" style={{ marginBottom: 7 }}>
            <select className="mono" value={leg.action} onChange={(e) => patch(i, { action: e.target.value })}>
              <option value="BUY">BUY</option>
              <option value="SELL">SELL</option>
            </select>
            <select
              className="mono"
              value={leg.kind}
              onChange={(e) => {
                const kind = e.target.value;
                patch(i, kind === "option"
                  ? { kind, option: leg.option ?? { right: "CE", strike_rule: "ATM+0", expiry: "current_weekly" } }
                  : { kind, option: undefined });
              }}
            >
              <option value="equity">equity</option>
              <option value="future">future</option>
              <option value="option">option</option>
            </select>
            <input
              className="mono"
              type="number"
              min={1}
              value={leg.lots ?? 1}
              onChange={(e) => patch(i, { lots: Number(e.target.value) })}
            />
          </div>

          {leg.kind === "option" && leg.option && (
            <div className="row" style={{ marginBottom: 7 }}>
              <select className="mono" value={leg.option.right}
                onChange={(e) => patch(i, { option: { ...leg.option, right: e.target.value } })}>
                <option value="CE">CE</option><option value="PE">PE</option>
              </select>
              <input className="mono" value={leg.option.strike_rule}
                onChange={(e) => patch(i, { option: { ...leg.option, strike_rule: e.target.value } })}
                placeholder="ATM+0" />
              <select className="mono" value={leg.option.expiry}
                onChange={(e) => patch(i, { option: { ...leg.option, expiry: e.target.value } })}>
                {["current_weekly", "next_weekly", "current_monthly", "next_monthly"].map((x) => (
                  <option key={x} value={x}>{x.replace("_", " ")}</option>
                ))}
              </select>
            </div>
          )}

          {(["stop_loss", "target", "trail"] as const).map((field) => (
            <ExitRow
              key={field}
              label={field.replace("_", " ")}
              value={leg[field]}
              atrIds={atrIds}
              onChange={(v) => setExit(i, field, v)}
            />
          ))}
        </div>
      ))}

      <button className="btn secondary tiny" onClick={add}>+ leg</button>
    </div>
  );
}

function ExitRow({
  label, value, atrIds, onChange,
}: {
  label: string; value: Doc | undefined; atrIds: string[]; onChange: (v: Doc | null) => void;
}) {
  if (!value) {
    return (
      <button className="btn secondary tiny" style={{ marginRight: 5, marginBottom: 4 }}
        onClick={() => onChange({ type: "percent", value: 1 })}>
        + {label}
      </button>
    );
  }

  return (
    <div className="row" style={{ marginBottom: 5, alignItems: "center" }}>
      <span className="hint" style={{ flex: "0 0 62px" }}>{label}</span>
      <select className="mono" value={value.type} onChange={(e) => {
        const type = e.target.value;
        onChange({ type, ref: type === "atr_multiple" ? (value.ref ?? atrIds[0]) : undefined });
      }}>
        <option value="percent">%</option>
        <option value="points">points</option>
        <option value="atr_multiple" disabled={atrIds.length === 0}>× ATR</option>
      </select>
      <input className="mono" type="number" step="0.1" value={value.value}
        onChange={(e) => onChange({ value: Number(e.target.value) })} />
      {value.type === "atr_multiple" && (
        <select className="mono" value={value.ref ?? ""} onChange={(e) => onChange({ ref: e.target.value })}>
          {atrIds.map((id) => <option key={id} value={id}>{id}</option>)}
        </select>
      )}
      <button className="remove" style={{ flex: "0 0 auto" }} onClick={() => onChange(null)}>×</button>
    </div>
  );
}

/* ------------------------------------------------------------------ */

function RiskPanel({
  doc, update, runConfig, setRunConfig,
}: {
  doc: Doc; update: (p: string[], v: unknown) => void;
  runConfig: RunConfig; setRunConfig: (c: RunConfig) => void;
}) {
  const risk = doc.risk ?? {};
  const session = doc.session ?? {};

  const setRisk = (key: string, value: unknown) => update(["risk"], { ...risk, [key]: value });
  const setSession = (key: string, value: unknown) => update(["session"], { ...session, [key]: value });

  return (
    <div className="panel">
      <div className="panel-title">Session &amp; risk</div>

      <div className="row">
        <div className="field">
          <label>start</label>
          <input className="mono" value={session.start ?? "09:20"} onChange={(e) => setSession("start", e.target.value)} />
        </div>
        <div className="field">
          <label>end</label>
          <input className="mono" value={session.end ?? "15:10"} onChange={(e) => setSession("end", e.target.value)} />
        </div>
        <div className="field">
          <label>square off</label>
          <input className="mono" value={session.square_off ?? ""} placeholder="none"
            onChange={(e) => setSession("square_off", e.target.value || null)} />
        </div>
      </div>

      <div className="row">
        <div className="field">
          <label>max trades/day</label>
          <input className="mono" type="number" value={risk.max_trades_per_day ?? ""}
            onChange={(e) => setRisk("max_trades_per_day", e.target.value ? Number(e.target.value) : null)} />
        </div>
        <div className="field">
          <label>max positions</label>
          <input className="mono" type="number" value={risk.max_concurrent_positions ?? 1}
            onChange={(e) => setRisk("max_concurrent_positions", Number(e.target.value))} />
        </div>
      </div>

      <div className="field">
        <label>max loss / day (₹, blank = none)</label>
        <input className="mono" type="number" value={risk.max_loss_per_day ?? ""}
          onChange={(e) => setRisk("max_loss_per_day", e.target.value ? Number(e.target.value) : null)} />
      </div>

      <div className="divider" />

      <div className="row">
        <div className="field">
          <label>capital (₹)</label>
          <input className="mono" type="number" value={runConfig.starting_capital}
            onChange={(e) => setRunConfig({ ...runConfig, starting_capital: Number(e.target.value) })} />
        </div>
        <div className="field">
          <label>qty per lot</label>
          <input className="mono" type="number" value={runConfig.lot_size}
            onChange={(e) => setRunConfig({ ...runConfig, lot_size: Number(e.target.value) })} />
        </div>
      </div>

      <div className="row">
        <div className="field">
          <label>slippage (bps)</label>
          <input className="mono" type="number" step="0.5" value={runConfig.slippage_bps}
            onChange={(e) => setRunConfig({ ...runConfig, slippage_bps: Number(e.target.value) })} />
        </div>
        <div className="field">
          <label>max leverage</label>
          <input className="mono" type="number" step="0.5" value={runConfig.max_leverage}
            onChange={(e) => setRunConfig({ ...runConfig, max_leverage: Number(e.target.value) })} />
        </div>
      </div>
      <div className="hint">
        Position notional above {runConfig.max_leverage}× equity is refused, so an
        undercapitalized run reports no trades rather than impossible returns.
      </div>
    </div>
  );
}
