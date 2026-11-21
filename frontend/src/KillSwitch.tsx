// The account kill switch (backend/live/kill_switch.py): one button, always in
// the header, that flattens and stops every paper session and portfolio and
// keeps new ones from starting until a person releases it.

import { useEffect, useState } from "react";
import { api, ApiError } from "./api";
import type { KillSwitchReport, KillSwitchState } from "./api";

const POLL_MS = 30_000;           // another tab, or the API, may engage it too
const ENGAGE_WORD = "FLATTEN";

export function useKillSwitch(onError: (m: string) => void) {
  const [state, setState] = useState<KillSwitchState | null>(null);
  const [report, setReport] = useState<KillSwitchReport | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    const load = () => api.killSwitch().then((s) => { if (alive) setState(s); }).catch(() => undefined);
    load();
    const timer = setInterval(load, POLL_MS);
    return () => { alive = false; clearInterval(timer); };
  }, []);

  const engage = async (reason: string) => {
    setBusy(true);
    try {
      const r = await api.engageKillSwitch(reason);
      setReport(r);
      setState(r);
    } catch (e) { onError((e as ApiError).message); } finally { setBusy(false); }
  };
  const release = async () => {
    setBusy(true);
    try {
      setState(await api.releaseKillSwitch());
      setReport(null);
    } catch (e) { onError((e as ApiError).message); } finally { setBusy(false); }
  };
  return { state, report, busy, engage, release };
}

type Kill = ReturnType<typeof useKillSwitch>;

/** Header: the button, and the confirmation that needs FLATTEN typed. */
export function KillSwitchButton({ kill }: { kill: Kill }) {
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const [reason, setReason] = useState("");
  const close = () => { setOpen(false); setTyped(""); setReason(""); };

  return (
    <>
      <button className="btn danger tiny" onClick={() => setOpen(true)} disabled={kill.busy}
        title="Flatten every paper position and stop all sessions">
        Flatten all
      </button>
      {open && (
        <div className="modal-backdrop" onClick={close}>
          <div className="modal" role="dialog" aria-modal="true" aria-labelledby="kill-title"
            onClick={(e) => e.stopPropagation()}>
            <div className="notice-title neg" id="kill-title">Kill switch</div>
            <p>
              Close every open paper position at its last price, stop every paper session and paper
              portfolio, and refuse new ones until you release the switch.
            </p>
            <label className="field">
              <span>Reason (optional)</span>
              <input type="text" value={reason} onChange={(e) => setReason(e.target.value)} maxLength={200} />
            </label>
            <label className="field">
              <span>Type {ENGAGE_WORD} to confirm</span>
              <input type="text" aria-label={`Type ${ENGAGE_WORD} to confirm`} value={typed} autoFocus
                onChange={(e) => setTyped(e.target.value)} />
            </label>
            <div className="row" style={{ gap: 8, justifyContent: "flex-end" }}>
              <button className="btn secondary" onClick={close}>Cancel</button>
              <button className="btn danger" disabled={typed !== ENGAGE_WORD || kill.busy}
                onClick={async () => { await kill.engage(reason.trim()); close(); }}>
                Flatten everything
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

/** Across the app while engaged: what happened, and a guarded Release. */
export function KillSwitchBanner({ kill }: { kill: Kill }) {
  const [confirming, setConfirming] = useState(false);
  const s = kill.state;
  if (!s?.engaged) return null;
  const r = kill.report;
  const closed = r ? r.sessions.reduce((n, x) => n + x.positions_closed, 0) : null;
  return (
    <div className="notice error kill-banner" role="alert">
      <div className="notice-title">Kill switch engaged</div>
      <div className="row" style={{ gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <span style={{ flex: "1 1 auto" }}>
          Engaged at {formatTime(s.engaged_at)}{s.reason ? ` (${s.reason})` : ""}: no new sessions can start.
          {r && ` ${r.sessions.length} paper session${r.sessions.length === 1 ? "" : "s"} stopped, ${closed} position${closed === 1 ? "" : "s"} closed, ${r.portfolios.length} paper portfolio${r.portfolios.length === 1 ? "" : "s"} stopped.`}
          {r && !r.complete && " Not everything closed: check the Live tab."}
        </span>
        {confirming ? (
          <span className="row" style={{ gap: 6 }}>
            <span>Allow sessions to start again?</span>
            <button className="btn tiny secondary" onClick={() => setConfirming(false)}>Cancel</button>
            <button className="btn tiny" disabled={kill.busy}
              onClick={async () => { await kill.release(); setConfirming(false); }}>Release</button>
          </span>
        ) : (
          <button className="btn tiny secondary" onClick={() => setConfirming(true)}>Release…</button>
        )}
      </div>
    </div>
  );
}

function formatTime(iso: string | null): string {
  if (!iso) return "–";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}
