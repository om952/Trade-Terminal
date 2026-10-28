// Live / paper session dashboard.
//
// This view is read-only by design. It can start a session and stop one; it
// cannot adjust a position, override a stop, or change a number the engine
// computed. Every figure shown here is copied from a snapshot the server built —
// if the UI ever calculated P&L itself, the screen and the journal could disagree,
// and the journal is the one that is true.
//
// Sessions are polled rather than streamed. A dashboard that misses a WebSocket
// message shows a stale position until the next one arrives; a poll that fails
// simply retries, and the session's own state is never in the browser to lose.

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";
import type { Dataset, InterruptedSession, LiveSnapshot, SessionRow, Trade } from "./api";

const POLL_MS = 2000;

const money = (n: number) =>
  n.toLocaleString("en-IN", { maximumFractionDigits: 2, minimumFractionDigits: 2 });

const signed = (n: number) => `${n >= 0 ? "+" : ""}${money(n)}`;

const clock = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleTimeString("en-IN", { hour12: false }) : "—";

export interface LiveConfig {
  symbol: string;
  starting_capital: number;
  lot_size: number;
  slippage_bps: number;
  max_leverage: number;
  source: "live" | "replay";
  replay_start: string;
  replay_end: string;
  replay_speed: number;
  alerts: boolean;
}

export const BLANK_LIVE_CONFIG: LiveConfig = {
  symbol: "",
  starting_capital: 1_000_000,
  lot_size: 1,
  slippage_bps: 5,
  max_leverage: 5,
  source: "replay",
  replay_start: "",
  replay_end: "",
  replay_speed: 0,
  alerts: true,
};

// ---------------------------------------------------------------------------
// Launcher
// ---------------------------------------------------------------------------

export function LivePanel({
  doc,
  datasets,
  config,
  setConfig,
  onStarted,
  valid,
}: {
  doc: Record<string, any>;
  datasets: Dataset[];
  config: LiveConfig;
  setConfig: (c: LiveConfig) => void;
  onStarted: (sessionId: string) => void;
  valid: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = <K extends keyof LiveConfig>(k: K, v: LiveConfig[K]) =>
    setConfig({ ...config, [k]: v });

  const dataset = datasets.find(
    (d) => d.instrument_key === config.symbol && d.timeframe === doc.timeframe,
  );

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await api.liveStart({
        strategy: doc,
        symbol: config.symbol || undefined,
        starting_capital: config.starting_capital,
        lot_size: config.lot_size,
        slippage_bps: config.slippage_bps,
        max_leverage: config.max_leverage,
        alerts: config.alerts,
        source: config.source,
        replay_start: config.source === "replay" ? config.replay_start || undefined : undefined,
        replay_end: config.source === "replay" ? config.replay_end || undefined : undefined,
        replay_speed: config.replay_speed,
      });
      onStarted(res.session_id);
    } catch (e) {
      setError((e as ApiError).message || "Could not start the session");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="panel">
      <div className="panel-title">Paper session</div>

      <div className="field">
        <label>Feed</label>
        <div className="seg-control">
          <button
            className={config.source === "replay" ? "active" : ""}
            onClick={() => set("source", "replay")}
          >
            Replay
          </button>
          <button
            className={config.source === "live" ? "active" : ""}
            onClick={() => set("source", "live")}
          >
            Live
          </button>
        </div>
        <span className="hint">
          {config.source === "replay"
            ? "Runs the identical live path over stored candles. Works outside market hours."
            : "Polls Upstox during the session. Needs a valid token and an open market."}
        </span>
      </div>

      {config.source === "replay" && (
        <>
          <div className="field-row">
            <div className="field">
              <label>From</label>
              <input
                type="date"
                value={config.replay_start}
                onChange={(e) => set("replay_start", e.target.value)}
              />
            </div>
            <div className="field">
              <label>To</label>
              <input
                type="date"
                value={config.replay_end}
                onChange={(e) => set("replay_end", e.target.value)}
              />
            </div>
          </div>
          <div className="field">
            <label>Speed</label>
            <select
              value={config.replay_speed}
              onChange={(e) => set("replay_speed", Number(e.target.value))}
            >
              <option value={0}>As fast as possible</option>
              <option value={600}>600× (a session in ~40s)</option>
              <option value={60}>60× (a session in ~6 min)</option>
              <option value={1}>Real time</option>
            </select>
          </div>
        </>
      )}

      <div className="field">
        <label>Telegram alerts</label>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={config.alerts}
            onChange={(e) => set("alerts", e.target.checked)}
          />
          <span>Notify on entry, exit and error</span>
        </label>
      </div>

      {dataset?.synthetic && (
        <div className="notice warn">
          <div className="notice-title">Simulated data</div>
          This dataset is generated, not real market history. It validates the
          plumbing, not the strategy.
        </div>
      )}

      {error && (
        <div className="notice error">
          <div className="notice-title">Could not start</div>
          {error}
        </div>
      )}

      <button className="btn primary wide" onClick={start} disabled={busy || !valid || !config.symbol}>
        {busy ? "Starting…" : config.source === "replay" ? "Start replay session" : "Start paper session"}
      </button>
      {!valid && <span className="hint">Fix the strategy errors first.</span>}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Dashboard
// ---------------------------------------------------------------------------

export function LiveDashboard({ sessionId, onPick }: { sessionId: string | null; onPick: (id: string) => void }) {
  const [snapshot, setSnapshot] = useState<LiveSnapshot | null>(null);
  const [sessions, setSessions] = useState<{ active: SessionRow[]; history: SessionRow[] }>({
    active: [],
    history: [],
  });
  const [error, setError] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);
  const timer = useRef<number | null>(null);

  const refreshList = useCallback(async () => {
    try {
      setSessions(await api.liveSessions());
    } catch {
      /* backend down; the poll below will surface it */
    }
  }, []);

  useEffect(() => {
    refreshList();
  }, [refreshList, sessionId]);

  useEffect(() => {
    if (!sessionId) {
      setSnapshot(null);
      return;
    }
    let cancelled = false;

    const poll = async () => {
      try {
        const snap = await api.liveSnapshot(sessionId, true);
        if (cancelled) return;
        setSnapshot(snap);
        setError(null);
        // Stop polling once the session is finished — there is nothing more to
        // learn, and a dashboard left open overnight should not keep hitting it.
        if (snap.running === false) {
          refreshList();
          return;
        }
      } catch (e) {
        if (!cancelled) setError((e as ApiError).message);
      }
      if (!cancelled) timer.current = window.setTimeout(poll, POLL_MS);
    };

    poll();
    return () => {
      cancelled = true;
      if (timer.current) window.clearTimeout(timer.current);
    };
  }, [sessionId, refreshList]);

  const stop = async () => {
    if (!sessionId) return;
    setStopping(true);
    try {
      await api.liveStop(sessionId);
      setSnapshot(await api.liveSnapshot(sessionId, true));
      refreshList();
    } catch (e) {
      setError((e as ApiError).message);
    } finally {
      setStopping(false);
    }
  };

  return (
    <div className="live">
      <InterruptedBanner onResumed={(id) => { refreshList(); onPick(id); }} />
      <SessionList sessions={sessions} current={sessionId} onPick={onPick} />

      {error && (
        <div className="notice error">
          <div className="notice-title">Cannot read the session</div>
          {error}
        </div>
      )}

      {!sessionId && (
        <div className="empty">
          <b>No session selected</b>
          <span>Start one from the sidebar, or pick a past session above.</span>
        </div>
      )}

      {snapshot && <SessionView snapshot={snapshot} onStop={stop} stopping={stopping} />}
    </div>
  );
}

// Sessions cut off by a crash or an app shutdown today. Offered, never resumed
// automatically: the user may have closed the position at the broker by hand,
// or may simply not want the session back.
function InterruptedBanner({ onResumed }: { onResumed: (sessionId: string) => void }) {
  const [sessions, setSessions] = useState<InterruptedSession[]>([]);
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.liveInterrupted().then((r) => setSessions(r.sessions)).catch(() => {});
  }, []);

  const visible = sessions.filter((s) => !dismissed.has(s.session_id));
  if (!visible.length) return null;

  const resume = async (s: InterruptedSession) => {
    setBusy(s.session_id);
    setError(null);
    try {
      const res = await api.liveStart(s.resume);
      setSessions((all) => all.filter((x) => x.session_id !== s.session_id));
      onResumed(res.session_id);
    } catch (e) {
      setError((e as ApiError).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="notice warn">
      <div className="notice-title">
        {visible.length === 1 ? "A paper session was" : `${visible.length} paper sessions were`} interrupted today
      </div>
      <ul>
        {visible.map((s) => (
          <li key={s.session_id}>
            <b>{s.strategy_name}</b> on {s.symbol.split("|").pop()} · stopped by {s.cause} ·{" "}
            {s.trades} trades{s.open_legs.length ? `, ${s.open_legs.length} open position(s)` : ""}{" "}
            <button className="btn tiny" onClick={() => resume(s)} disabled={busy !== null}>
              {busy === s.session_id ? "Resuming…" : "Resume"}
            </button>{" "}
            <button
              className="btn tiny secondary"
              onClick={() => setDismissed((d) => new Set(d).add(s.session_id))}
              disabled={busy !== null}
            >
              Dismiss
            </button>
          </li>
        ))}
      </ul>
      {error && <div>{error}</div>}
    </div>
  );
}

function SessionList({
  sessions,
  current,
  onPick,
}: {
  sessions: { active: SessionRow[]; history: SessionRow[] };
  current: string | null;
  onPick: (id: string) => void;
}) {
  const rows = [...sessions.active, ...sessions.history];
  if (!rows.length) return null;

  return (
    <div className="session-strip">
      {rows.map((row) => (
        <button
          key={row.session_id}
          className={`session-pill ${row.session_id === current ? "active" : ""}`}
          onClick={() => onPick(row.session_id)}
          title={row.session_id}
        >
          <span className={`dot ${row.running ? "live" : row.state === "interrupted" ? "warn" : "ok"}`} />
          <span className="pill-name">{row.strategy}</span>
          <span className="pill-meta">
            {row.symbol.split("|").pop()} · {row.trades} trades
          </span>
        </button>
      ))}
    </div>
  );
}

function SessionView({
  snapshot,
  onStop,
  stopping,
}: {
  snapshot: LiveSnapshot;
  onStop: () => void;
  stopping: boolean;
}) {
  const s = snapshot;
  const live = s.running === true;
  const pnlClass = s.net_pnl >= 0 ? "up" : "down";

  return (
    <>
      <div className="live-header">
        <div>
          <div className="live-title">
            <span className={`dot ${live ? "live" : "ok"}`} />
            {s.strategy?.name ?? "Session"}
            <span className="live-sub">
              {s.symbol?.split("|").pop()} · {s.strategy?.timeframe} · {s.mode}
            </span>
          </div>
          <div className="live-sub">
            {s.session_id}
            {s.resumed && <span className="tag warn">resumed</span>}
            {s.from_journal && <span className="tag">from journal</span>}
          </div>
        </div>

        <div className="live-actions">
          <span className="status-chip">
            <span className={`dot ${s.feed?.connected ? "ok" : "warn"}`} />
            feed {s.feed?.connected ? "connected" : "idle"}
            {s.feed?.reconnects ? <b> · {s.feed.reconnects} reconnects</b> : null}
          </span>
          <span className="status-chip">
            <span className={`dot ${s.reconciled ? "ok" : "bad"}`} />
            {s.reconciled ? "reconciled" : "MISMATCH"}
          </span>
          {live && (
            <button className="btn danger" onClick={onStop} disabled={stopping}>
              {stopping ? "Stopping…" : "Stop session"}
            </button>
          )}
        </div>
      </div>

      {!s.reconciled && (
        <div className="notice error">
          <div className="notice-title">Position mismatch — trading halted</div>
          The engine and the broker ledger disagree about what is held. The session
          stopped rather than act on a guess. Check the journal before restarting.
        </div>
      )}

      {s.risk?.halted && (
        <div className="notice warn">
          <div className="notice-title">Risk limit tripped: {s.risk.reason}</div>
          No further entries this session. This is sticky until the next day by design.
        </div>
      )}

      {(s.errors?.length ?? 0) > 0 && (
        <div className="notice error">
          <div className="notice-title">Errors</div>
          {s.errors.map((e, i) => (
            <div key={i}>{e}</div>
          ))}
        </div>
      )}

      {(s.warnings?.length ?? 0) > 0 && (
        <div className="notice warn">
          <div className="notice-title">Preflight warnings</div>
          {s.warnings.map((w, i) => (
            <div key={i}>{w}</div>
          ))}
        </div>
      )}

      <div className="metric-grid">
        <Metric label="Net P&L" value={signed(s.net_pnl)} cls={pnlClass} big />
        <Metric label="Equity" value={money(s.equity)} />
        <Metric label="Realized" value={signed(s.realized_pnl)} cls={s.realized_pnl >= 0 ? "up" : "down"} />
        <Metric
          label="Unrealized"
          value={signed(s.unrealized_pnl)}
          cls={s.unrealized_pnl >= 0 ? "up" : "down"}
        />
        <Metric label="Fees" value={money(s.fees_paid)} cls="down" />
        <Metric label="Trades" value={String(s.trades?.length ?? 0)} />
        <Metric label="Bars" value={String(s.bars_seen)} />
        <Metric label="Last bar" value={clock(s.last_bar?.t ?? s.feed?.last_bar_at)} />
      </div>

      <OpenPositions snapshot={s} />

      {Object.keys(s.blocked ?? {}).length > 0 && (
        <section className="panel">
          <div className="panel-title">Entries blocked</div>
          <div className="chip-row">
            {Object.entries(s.blocked).map(([reason, count]) => (
              <span key={reason} className="chip">
                {reason.replace(/_/g, " ")} <b>{count}</b>
              </span>
            ))}
          </div>
          <span className="hint">
            Why a signal fired but no order followed. Silence here would look
            identical to a strategy that found nothing.
          </span>
        </section>
      )}

      <TradeTable trades={s.trades ?? []} />
    </>
  );
}

function Metric({
  label,
  value,
  cls,
  big,
}: {
  label: string;
  value: string;
  cls?: string;
  big?: boolean;
}) {
  return (
    <div className={`metric ${big ? "metric-big" : ""}`}>
      <div className="metric-label">{label}</div>
      <div className={`metric-value ${cls ?? ""}`}>{value}</div>
    </div>
  );
}

function OpenPositions({ snapshot }: { snapshot: LiveSnapshot }) {
  const positions = snapshot.open_positions ?? [];
  return (
    <section className="panel">
      <div className="panel-title">Open positions</div>
      {positions.length === 0 ? (
        <div className="muted">Flat.</div>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Leg</th>
              <th>Side</th>
              <th className="num">Qty</th>
              <th className="num">Entry</th>
              <th className="num">Last</th>
              <th className="num">Stop</th>
              <th className="num">Target</th>
              <th className="num">Unrealized</th>
              <th>Since</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((p) => (
              <tr key={p.leg_id}>
                <td>{p.leg_id}</td>
                <td>
                  <span className={`tag ${p.direction === "LONG" ? "long" : "short"}`}>
                    {p.direction}
                  </span>
                </td>
                <td className="num">{Math.abs(p.quantity)}</td>
                <td className="num">{money(p.average_price)}</td>
                <td className="num">{money(p.last_price)}</td>
                <td className="num">{p.stop_price != null ? money(p.stop_price) : "—"}</td>
                <td className="num">{p.target_price != null ? money(p.target_price) : "—"}</td>
                <td className={`num ${p.unrealized_pnl >= 0 ? "up" : "down"}`}>
                  {signed(p.unrealized_pnl)}
                </td>
                <td>{clock(p.opened_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function TradeTable({ trades }: { trades: Trade[] }) {
  // Newest first: during a session the last trade is the one being looked for.
  const rows = [...trades].reverse();

  return (
    <section className="panel">
      <div className="panel-title">Trades ({trades.length})</div>
      {rows.length === 0 ? (
        <div className="muted">No trades yet.</div>
      ) : (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th>Entry</th>
                <th>Exit</th>
                <th>Side</th>
                <th className="num">Qty</th>
                <th className="num">In</th>
                <th className="num">Out</th>
                <th className="num">Fees</th>
                <th className="num">Net</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((t, i) => (
                <tr key={`${t.entry_time}-${i}`}>
                  <td>{clock(t.entry_time)}</td>
                  <td>{clock(t.exit_time)}</td>
                  <td>
                    <span className={`tag ${t.direction === "LONG" ? "long" : "short"}`}>
                      {t.direction}
                    </span>
                  </td>
                  <td className="num">{t.quantity}</td>
                  <td className="num">{money(t.entry_price)}</td>
                  <td className="num">{money(t.exit_price)}</td>
                  <td className="num down">{money(t.fees)}</td>
                  <td className={`num ${t.net_pnl >= 0 ? "up" : "down"}`}>{signed(t.net_pnl)}</td>
                  <td>{t.reason.replace(/_/g, " ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
