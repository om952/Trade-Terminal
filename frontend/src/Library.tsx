// Saved strategies, their version history, and their backtest runs.

import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "./api";
import type { RunRow, SavedStrategy, StrategyVersion } from "./api";

export function Library({ onOpenDocument, onOpenRun }: {
  onOpenDocument: (document: Record<string, unknown>, strategyId: string) => void;
  onOpenRun: (runId: string) => void;
}) {
  const [items, setItems] = useState<SavedStrategy[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [versions, setVersions] = useState<StrategyVersion[]>([]);
  const [runs, setRuns] = useState<RunRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try { setItems((await api.strategies()).strategies); } catch (e) { setError((e as ApiError).message); }
  }, []);
  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    if (!selected) return;
    (async () => {
      try {
        const [detail, history] = await Promise.all([api.strategy(selected), api.runs(selected)]);
        setVersions(detail.versions);
        setRuns(history.runs);
      } catch (e) {
        setError((e as ApiError).message);
      }
    })();
  }, [selected]);

  const open = async (id: string, version?: number) => {
    const detail = await api.strategy(id, version);
    onOpenDocument(detail.document, id);
  };

  const archive = async (id: string) => {
    await api.archiveStrategy(id);
    setSelected(null);
    load();
  };

  return (
    <div className="panel">
      <div className="panel-title">
        <span>Saved strategies</span>
        <button className="btn secondary tiny" onClick={load}>Refresh</button>
      </div>
      {error && <div className="notice error">{error}</div>}
      {items.length === 0 && <p className="hint">Nothing saved yet. Approve a strategy in the assistant, or save one from the builder.</p>}
      <ul className="library">
        {items.map((s) => (
          <li key={s.id} className={selected === s.id ? "active" : ""}>
            <button className="link" onClick={() => setSelected(selected === s.id ? null : s.id)}>
              <b>{s.name}</b> <span className="hint">v{s.version} · {s.runs} run(s)</span>
            </button>
            {selected === s.id && (
              <div className="library-detail">
                <div className="row" style={{ gap: 6 }}>
                  <button className="btn tiny" onClick={() => open(s.id)}>Open latest</button>
                  <button className="btn danger tiny" onClick={() => archive(s.id)}>Archive</button>
                </div>
                <div className="readback-heading">Versions</div>
                <ul>
                  {versions.map((v) => (
                    <li key={v.version}>
                      <button className="link" onClick={() => open(s.id, v.version)}>
                        v{v.version} · {v.origin} · {v.created_at.slice(0, 16).replace("T", " ")}
                      </button>
                      {v.note && <span className="hint"> · {v.note}</span>}
                    </li>
                  ))}
                </ul>
                <div className="readback-heading">Runs</div>
                {runs.length === 0 && <p className="hint">No runs yet.</p>}
                <ul>
                  {runs.map((r) => (
                    <li key={r.id}>
                      <button className="link" onClick={() => onOpenRun(r.id)}>
                        {r.created_at.slice(0, 16).replace("T", " ")} · {r.data.instrument_key} {r.data.timeframe} ·{" "}
                        <span className={r.metrics.net_pnl >= 0 ? "pos" : "neg"}>
                          {r.metrics.net_pnl?.toLocaleString(undefined, { maximumFractionDigits: 0 })}
                        </span>{" "}
                        · {r.metrics.total_trades} trades
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
