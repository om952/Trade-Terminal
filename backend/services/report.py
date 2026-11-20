"""A backtest run as one self-contained HTML file (P3.5's "get report").

The file holds what a reader needs to judge the run without the app: the
strategy in words (the same read-back the builder shows), where the data came
from and how much of it there was, the metrics, how many times this idea has
been tried, the equity curve, and the trades. It has no scripts and no
external requests, so it opens anywhere and can be attached to an email.

Every value is HTML-escaped: strategy names and descriptions may be written by
a model or pasted from elsewhere.
"""

from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any

from backend.core.config import settings

_METRICS = (
    ("net_pnl", "Net P&L", "{:,.0f}"), ("total_return_pct", "Return", "{:.2f}%"),
    ("cagr_pct", "CAGR", "{:.2f}%"), ("max_drawdown_pct", "Max drawdown", "{:.2f}%"),
    ("sharpe", "Sharpe", "{:.2f}"), ("sortino", "Sortino", "{:.2f}"),
    ("profit_factor", "Profit factor", "{:.2f}"), ("win_rate_pct", "Win rate", "{:.1f}%"),
    ("total_trades", "Trades", "{:,}"), ("total_fees", "Fees", "{:,.0f}"),
    ("exposure_pct", "Time in market", "{:.1f}%"),
)


def reports_dir() -> Path:
    path = settings.data_dir / "reports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _fmt(value: Any, pattern: str) -> str:
    if value is None:
        return "–"
    try:
        return pattern.format(value)
    except (ValueError, TypeError):
        return escape(str(value))


def _equity_svg(curve: list[dict[str, Any]], width: int = 760, height: int = 180) -> str:
    points = [p["equity"] for p in curve if p.get("equity") is not None]
    if len(points) < 2:
        return "<p class='muted'>No equity curve.</p>"
    step = max(1, len(points) // 600)          # at most ~600 points in the file
    points = points[::step]
    lo, hi = min(points), max(points)
    span = (hi - lo) or 1.0
    xy = " ".join(
        f"{i * (width - 2) / (len(points) - 1) + 1:.1f},{height - 1 - (v - lo) / span * (height - 2):.1f}"
        for i, v in enumerate(points)
    )
    return (f"<svg viewBox='0 0 {width} {height}' width='100%' role='img' aria-label='Equity curve'>"
            f"<polyline fill='none' stroke='#2563eb' stroke-width='1.5' points='{xy}'/></svg>"
            f"<p class='muted'>Equity from {lo:,.0f} to {hi:,.0f}.</p>")


def render(run: dict[str, Any], card: dict[str, Any] | None, trials: dict[str, Any] | None) -> str:
    result = run.get("result") or {}
    metrics = result.get("metrics") or run.get("metrics") or {}
    data = result.get("data") or run.get("data") or {}
    trades = result.get("trades") or []
    name = escape(run.get("strategy_name") or "Backtest")
    rows = "".join(
        f"<tr><th>{escape(label)}</th><td>{_fmt(metrics.get(key), pattern)}</td></tr>"
        for key, label, pattern in _METRICS
    )
    sections = ""
    if card:
        for s in card.get("sections", []):
            items = "".join(f"<li>{escape(str(line))}</li>" for line in s.get("lines", []))
            sections += f"<h3>{escape(s.get('heading', ''))}</h3><ul>{items}</ul>"
    notes = [escape(w) for w in result.get("warnings") or []]
    if result.get("synthetic_data"):
        notes.insert(0, "Synthetic data: this result says nothing about real markets.")
    if trials and trials.get("runs", 0) > 1:
        notes.append(f"This idea has had {trials['runs']} backtests across {trials['variants']} variant(s); "
                     "the more variants tried, the more of the best result is luck.")
    trade_rows = "".join(
        "<tr>" + "".join(f"<td>{escape(str(t.get(k, '')))}</td>" for k in
                         ("entry_time", "exit_time", "direction", "quantity", "entry_price", "exit_price",
                          "net_pnl", "reason")) + "</tr>"
        for t in trades[:2000]
    )
    more = f"<p class='muted'>Showing 2,000 of {len(trades):,} trades.</p>" if len(trades) > 2000 else ""
    note_list = "".join(f"<li>{n}</li>" for n in notes)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name} · backtest report</title>
<style>
body {{ font: 14px/1.5 system-ui, sans-serif; color: #111; background: #fff; max-width: 880px; margin: 24px auto; padding: 0 16px; }}
h1 {{ font-size: 22px; margin-bottom: 4px; }} h2 {{ font-size: 17px; margin-top: 28px; }} h3 {{ font-size: 14px; margin: 12px 0 4px; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; }} th, td {{ text-align: left; padding: 4px 8px; border-bottom: 1px solid #e5e7eb; }}
.metrics th {{ width: 40%; font-weight: 500; color: #374151; }} .muted {{ color: #6b7280; font-size: 12px; }}
.trades {{ display: block; overflow-x: auto; }} .notes li {{ color: #92400e; }}
@media (prefers-color-scheme: dark) {{ body {{ background: #0f1115; color: #e5e7eb; }} th, td {{ border-color: #2a2f3a; }} .metrics th {{ color: #9ca3af; }} }}
</style></head><body>
<h1>{name}</h1>
<p class="muted">Run {escape(str(run.get('id', '')))} · {escape(str(run.get('created_at', '')))} · strategy {escape(str(run.get('strategy_hash', ''))[:12])}</p>
{f'<ul class="notes">{note_list}</ul>' if note_list else ''}
<h2>The strategy</h2>{sections or '<p class="muted">No read-back available.</p>'}
<h2>Data</h2>
<p>{escape(str(data.get('instrument_key', result.get('symbol', ''))))} · {escape(str(data.get('timeframe', result.get('timeframe', ''))))} · {escape(str(data.get('source', '')))}
{escape(' (derived from ' + data['derived_from'] + ')') if data.get('derived_from') else ''}<br>
{escape(str(data.get('first_bar', ''))[:10])} to {escape(str(data.get('last_bar', ''))[:10])} · {int(data.get('bars') or 0):,} bars · {int(data.get('missing_sessions') or 0)} missing session(s) · fingerprint {escape(str(data.get('fingerprint', ''))[:12])}</p>
<h2>Results</h2><table class="metrics">{rows}</table>
<h2>Equity</h2>{_equity_svg(result.get('equity_curve') or [])}
<h2>Trades</h2>
<table class="trades"><thead><tr><th>Entry</th><th>Exit</th><th>Side</th><th>Qty</th><th>Entry price</th><th>Exit price</th><th>Net P&amp;L</th><th>Reason</th></tr></thead>
<tbody>{trade_rows}</tbody></table>{more}
<p class="muted">Backtests carry costs and slippage but not every real-world friction. This is not investment advice.</p>
</body></html>
"""


def write(run_id: str) -> dict[str, Any]:
    """Write the report for a run and return its path and headline numbers."""
    from backend.agent import readback
    from backend.appstate import runs
    from backend.strategy.family import family_key
    from backend.strategy.schema import Strategy

    run = runs.get(run_id, include_result=True)
    try:
        card = readback.render(Strategy.model_validate(run["document"]))
    except Exception:  # noqa: BLE001 - a document an older build wrote
        card = None
    family = run.get("family") or family_key(run["document"])
    trials = runs.family_trials(family) if family else None
    path = reports_dir() / f"{run_id}.html"
    path.write_text(render(run, card, trials), encoding="utf-8")
    metrics = (run.get("result") or {}).get("metrics") or run.get("metrics") or {}
    return {
        "run_id": run_id, "path": str(path),
        "headline": {k: metrics.get(k) for k in ("net_pnl", "total_return_pct", "max_drawdown_pct",
                                                  "sharpe", "win_rate_pct", "total_trades")},
        "synthetic_data": bool((run.get("result") or {}).get("synthetic_data")),
        "family_trials": trials,
    }
