// Paper portfolios: a portfolio run forward one session close at a time
// (backend/portfolio/session.py). Each recorded day is the paper record and is
// never rewritten; the next open's orders are what a trader would place.

import { useEffect, useState } from "react";
import { api, ApiError, TERMINAL_JOB } from "./api";
import type { Job, PortfolioSessionDetail, PortfolioSessionSummary, PortfolioStep } from "./api";
import { EquityLine, fmt } from "./Portfolio";
import { useJobPoll } from "./Screener";

const STATUS_LABEL: Record<string, string> = { active: "active", stopped: "stopped", halted: "halted" };

export function PaperPortfolios({ focus, setFocus, version, startJob, onError }: {
  focus: string | null;
  /** The first step of a session just started, to follow. */
  startJob?: Job<PortfolioStep> | null;
  setFocus: (id: string | null) => void;
  /** Bump to reload (after a session is started elsewhere). */
  version: number;
  onError: (m: string) => void;
}) {
  const [list, setList] = useState<PortfolioSessionSummary[] | null>(null);
  const [times, setTimes] = useState("");
  const [detail, setDetail] = useState<PortfolioSessionDetail | null>(null);
  const [job, setJob] = useState<Job<PortfolioStep> | null>(null);
  useJobPoll(job, setJob, onError);
  const stepping = job != null && !TERMINAL_JOB.includes(job.status);

  const load = async () => {
    try {
      const listed = await api.portfolioSessions();
      setList(listed.sessions);
      setTimes(listed.step_times);
      setDetail(focus ? await api.portfolioSession(focus) : null);
    } catch (e) { onError((e as ApiError).message); }
  };
  useEffect(() => { load(); }, [focus, version]);
  useEffect(() => { if (startJob) setJob(startJob); }, [startJob]);
  useEffect(() => { if (job && TERMINAL_JOB.includes(job.status)) load(); }, [job?.status]);

  const step = async () => {
    if (!detail) return;
    try { setJob(await api.stepPortfolioSession(detail.id)); } catch (e) { onError((e as ApiError).message); }
  };
  const stop = async () => {
    if (!detail) return;
    try { setDetail(await api.stopPortfolioSession(detail.id)); load(); } catch (e) { onError((e as ApiError).message); }
  };

  if (list == null) return null;
  const s = detail?.state;
  return (
    <section aria-label="Paper portfolios" style={{ marginTop: 24 }}>
      <h3 className="section-title">Paper portfolios ({list.length})</h3>
      <p className="hint">
        Each active portfolio steps to the previous session's close at {times || "the scheduled time"} IST, once the
        daily bars are published, or when you press Update now. Recorded days are never rewritten. Paper only: live
        portfolio orders wait for Phase 5.
      </p>
      {list.length === 0 ? <p className="hint">None yet. Backtest a portfolio, then paper trade it from the result.</p> : (
        <div className="table-wrap">
          <table style={{ minWidth: 0 }} aria-label="Paper portfolio sessions">
            <thead><tr><th>Portfolio</th><th>Status</th><th>Started</th><th>Last close</th><th>Equity</th><th>Return</th></tr></thead>
            <tbody>
              {list.map((p) => (
                <tr key={p.id} className={p.id === focus ? "selected" : ""}>
                  <td><button className="btn-link" onClick={() => setFocus(p.id)}>{p.name}</button></td>
                  <td className={p.status === "halted" ? "neg" : p.status === "active" ? "pos" : ""}>{STATUS_LABEL[p.status]}</td>
                  <td className="mono">{p.start_date}</td>
                  <td className="mono">{p.last_bar_date ?? "–"}</td>
                  <td className="mono">{fmt(p.equity, 0)}</td>
                  <td className={`mono ${(p.return_pct ?? 0) > 0 ? "pos" : (p.return_pct ?? 0) < 0 ? "neg" : ""}`}>
                    {p.return_pct == null ? "–" : `${fmt(p.return_pct)}%`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {detail && (
        <div className="panel" style={{ marginTop: 12 }} aria-label="Paper portfolio">
          <div className="row" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
            <b style={{ flex: "1 1 auto" }}>{detail.name}</b>
            <span className="hint" style={{ flex: "0 0 auto" }}>
              {detail.members} stocks{detail.benchmark ? ` · filter on ${detail.benchmark}` : ""} · started {detail.start_date}
              {" "}with ₹{fmt(detail.starting_capital, 0)}
            </span>
          </div>
          {detail.status_reason && (
            <div className={`notice ${detail.status === "halted" ? "error" : ""}`}>{detail.status_reason}</div>
          )}
          <div className="row" style={{ gap: 8, margin: "8px 0" }}>
            <button className="btn" disabled={detail.status !== "active" || stepping} onClick={step}>
              {stepping ? <><span className="spinner" /> Updating…</> : "Update now"}
            </button>
            <button className="btn secondary" disabled={detail.status !== "active"} onClick={stop}>Stop</button>
            {job?.status === "failed" && <span className="neg">{job.error}</span>}
            {job?.status === "succeeded" && job.result && (
              <span className="hint" role="status">
                {job.result.new_days ? `Recorded ${job.result.new_days} new session${job.result.new_days === 1 ? "" : "s"}.`
                  : "No new session close yet."}
              </span>
            )}
          </div>

          {!s?.as_of ? (
            <p className="hint">Waiting for the first session close on or after {detail.start_date}. The portfolio
              is bought at the open after that close.</p>
          ) : (
            <>
              <div className="metric-grid" aria-label="Paper portfolio results">
                {([["Equity", s.equity, 0], ["Net P&L", s.net_pnl, 0], ["Realized", s.realized_pnl, 0],
                  ["Open", s.unrealized_pnl, 0], ["Return", s.return_pct, 2, "%"], ["Max drawdown", s.max_drawdown_pct, 2, "%"],
                  ["Cash", s.cash, 0]] as [string, number, number, string?][]).map(([label, value, digits, unit]) => (
                  <div className="metric" key={label}>
                    <div className="metric-label">{label}</div>
                    <div className={`metric-value ${label.startsWith("Net") || label === "Return" ? (value > 0 ? "pos" : value < 0 ? "neg" : "") : ""}`}>
                      {fmt(value, digits)}{unit ?? ""}
                    </div>
                  </div>
                ))}
              </div>
              <p className="hint">As of the close of {s.as_of}.</p>

              <h3 className="section-title">Orders for the next open ({s.pending.length})</h3>
              {s.pending.length === 0 ? <p className="hint">None: the next orders come at the next rebalance.</p> : (
                <div className="table-wrap">
                  <table style={{ minWidth: 0 }} aria-label="Orders for the next open">
                    <thead><tr><th>Side</th><th>Stock</th><th>Quantity</th><th>Why</th></tr></thead>
                    <tbody>
                      {s.pending.map((o) => (
                        <tr key={`${o.side}-${o.symbol}`}>
                          <td className={o.side === "BUY" ? "pos" : "neg"}>{o.side}</td><td><b>{o.symbol}</b></td>
                          <td className="mono">{o.quantity}</td><td className="hint">{o.reason}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              <h3 className="section-title">Holding ({s.holdings.length})</h3>
              {s.holdings.length === 0 ? <p className="hint">In cash.</p> : (
                <div className="table-wrap">
                  <table style={{ minWidth: 0 }} aria-label="Paper holdings">
                    <thead><tr><th>Stock</th><th>Quantity</th><th>Average price</th><th>Last close</th><th>Since</th></tr></thead>
                    <tbody>
                      {s.holdings.map((h) => (
                        <tr key={h.symbol}><td><b>{h.symbol}</b></td><td className="mono">{h.quantity}</td>
                          <td className="mono">{fmt(h.avg_price)}</td><td className="mono">{fmt(h.last_close)}</td>
                          <td className="mono">{h.since}</td></tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              <h3 className="section-title">Equity</h3>
              <EquityLine points={detail.days} />

              <h3 className="section-title">Daily record ({detail.days.length})</h3>
              <div className="table-wrap table-scroll">
                <table style={{ minWidth: 0 }} aria-label="Daily record">
                  <thead><tr><th>Close of</th><th>Equity</th><th>Cash</th><th>Filled at that open</th></tr></thead>
                  <tbody>
                    {[...detail.days].reverse().map((d) => (
                      <tr key={d.day}>
                        <td className="mono">{d.day}</td><td className="mono">{fmt(d.equity, 0)}</td>
                        <td className="mono">{fmt(d.cash, 0)}</td>
                        <td className="mono">{d.fills.map((f) => `${f.side} ${f.quantity} ${f.symbol} @ ${fmt(f.price)}`).join("; ") || "–"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {s.missing.length > 0 && (
                <details style={{ marginTop: 8 }}>
                  <summary className="hint">{s.missing.length} not loaded</summary>
                  <ul className="caveats">{s.missing.map((x) => <li key={x.symbol}><b>{x.symbol}</b>: {x.reason}</li>)}</ul>
                </details>
              )}
            </>
          )}
        </div>
      )}
    </section>
  );
}
