// Result views: Chart, Performance, Trade Log.
//
// These render what the backend computed. No metric is recalculated here — a
// number shown in the UI that was derived in the browser could disagree with the
// same number in a CSV export or a live session.

import { useEffect, useMemo, useRef, useState } from "react";
import { createChart, ColorType, type IChartApi } from "lightweight-charts";
import type { BacktestResult, Metrics, Trade } from "./api";

const fmt = (n: number, digits = 2) =>
  Number.isFinite(n) ? n.toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits }) : "—";

const money = (n: number) =>
  Number.isFinite(n) ? `₹${Math.round(n).toLocaleString("en-IN")}` : "—";

const pct = (n: number) => (Number.isFinite(n) ? `${n >= 0 ? "+" : ""}${n.toFixed(2)}%` : "—");

function chartTheme() {
  const css = getComputedStyle(document.documentElement);
  const read = (name: string) => css.getPropertyValue(name).trim();
  return {
    background: read("--surface"),
    text: read("--muted"),
    grid: read("--border"),
    up: read("--long"),
    down: read("--short"),
    accent: read("--accent"),
  };
}

/* ================================================================== */
/* Chart                                                               */
/* ================================================================== */

export function ChartTab({ result }: { result: BacktestResult }) {
  const priceRef = useRef<HTMLDivElement>(null);
  const paneRef = useRef<HTMLDivElement>(null);
  const chartsRef = useRef<IChartApi[]>([]);

  useEffect(() => {
    if (!result.chart || !priceRef.current) return;
    const theme = chartTheme();
    const { candles, overlays, panes, markers } = result.chart;

    const common = {
      layout: { background: { type: ColorType.Solid, color: theme.background }, textColor: theme.text },
      grid: { vertLines: { color: theme.grid }, horzLines: { color: theme.grid } },
      rightPriceScale: { borderColor: theme.grid },
      timeScale: { borderColor: theme.grid, timeVisible: true, secondsVisible: false },
      crosshair: { mode: 0 as const },
    };

    const price = createChart(priceRef.current, {
      ...common,
      height: 420,
      width: priceRef.current.clientWidth,
    });

    const candleSeries = price.addCandlestickSeries({
      upColor: theme.up, downColor: theme.down,
      borderUpColor: theme.up, borderDownColor: theme.down,
      wickUpColor: theme.up, wickDownColor: theme.down,
    });
    candleSeries.setData(candles as never);
    if (markers.length) candleSeries.setMarkers(markers as never);

    const palette = ["#E09A4B", "#4B9FE0", "#B07FE0", "#7FE0A8", "#E0C64B", "#E07F9F"];
    overlays.forEach((line, i) => {
      const series = price.addLineSeries({
        color: palette[i % palette.length],
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        title: line.id,
      });
      series.setData(line.data as never);
    });

    chartsRef.current = [price];

    // Oscillators get their own pane; drawn on the price scale they would
    // flatten the candles into a line.
    if (panes.length && paneRef.current) {
      const oscillator = createChart(paneRef.current, {
        ...common,
        height: 150,
        width: paneRef.current.clientWidth,
      });
      panes.forEach((line, i) => {
        const series = oscillator.addLineSeries({
          color: palette[(i + overlays.length) % palette.length],
          lineWidth: 1,
          priceLineVisible: false,
          title: line.id,
        });
        series.setData(line.data as never);
      });
      chartsRef.current.push(oscillator);

      // Keep the panes' time axes locked together while panning.
      const sync = (source: IChartApi, target: IChartApi) => {
        source.timeScale().subscribeVisibleLogicalRangeChange((range) => {
          if (range) target.timeScale().setVisibleLogicalRange(range);
        });
      };
      sync(price, oscillator);
      sync(oscillator, price);
    }

    const onResize = () => {
      chartsRef.current.forEach((c, i) => {
        const el = i === 0 ? priceRef.current : paneRef.current;
        if (el) c.applyOptions({ width: el.clientWidth });
      });
    };
    window.addEventListener("resize", onResize);

    return () => {
      window.removeEventListener("resize", onResize);
      chartsRef.current.forEach((c) => c.remove());
      chartsRef.current = [];
    };
  }, [result]);

  if (!result.chart) return <div className="empty">Chart data was not requested for this run.</div>;

  return (
    <>
      {result.chart.truncated && (
        <div className="notice info">
          <div className="notice-title">Showing the most recent {result.chart.candles.length.toLocaleString()} bars</div>
          The backtest ran over all {result.chart.total_bars.toLocaleString()} bars; the chart is
          truncated rather than thinned, so every candle shown is real.
        </div>
      )}
      <div className="chart-box" style={{ marginBottom: 12 }}>
        <div className="chart-head">
          <span>{result.symbol} · {result.timeframe}</span>
          <span>{result.chart.markers.length / 2} trades marked</span>
        </div>
        <div ref={priceRef} />
      </div>
      {result.chart.panes.length > 0 && (
        <div className="chart-box">
          <div className="chart-head"><span>{result.chart.panes.map((p) => p.id).join(" · ")}</span></div>
          <div ref={paneRef} />
        </div>
      )}
    </>
  );
}

/* ================================================================== */
/* Performance                                                         */
/* ================================================================== */

export function PerformanceTab({ result }: { result: BacktestResult }) {
  const equityRef = useRef<HTMLDivElement>(null);
  const ddRef = useRef<HTMLDivElement>(null);
  const chartsRef = useRef<IChartApi[]>([]);
  const m = result.metrics;

  useEffect(() => {
    if (!equityRef.current || !result.equity_curve.length) return;
    const theme = chartTheme();
    const common = {
      layout: { background: { type: ColorType.Solid, color: theme.background }, textColor: theme.text },
      grid: { vertLines: { color: theme.grid }, horzLines: { color: theme.grid } },
      rightPriceScale: { borderColor: theme.grid },
      timeScale: { borderColor: theme.grid, timeVisible: true, secondsVisible: false },
    };

    const equity = createChart(equityRef.current, { ...common, height: 260, width: equityRef.current.clientWidth });
    const line = equity.addAreaSeries({
      lineColor: theme.accent,
      topColor: `${theme.accent}55`,
      bottomColor: `${theme.accent}05`,
      lineWidth: 2,
    });
    line.setData(result.equity_curve.map((p) => ({ time: p.time, value: p.equity })) as never);

    // A reference line at starting capital: above it is profit, below is loss.
    const baseline = equity.addLineSeries({
      color: theme.grid, lineWidth: 1, lineStyle: 2,
      priceLineVisible: false, lastValueVisible: false,
    });
    baseline.setData(
      result.equity_curve.map((p) => ({ time: p.time, value: m.starting_capital })) as never,
    );

    chartsRef.current = [equity];

    if (ddRef.current) {
      const dd = createChart(ddRef.current, { ...common, height: 150, width: ddRef.current.clientWidth });
      const area = dd.addAreaSeries({
        lineColor: theme.down, topColor: `${theme.down}10`, bottomColor: `${theme.down}55`, lineWidth: 1,
      });
      area.setData(result.equity_curve.map((p) => ({ time: p.time, value: p.drawdown })) as never);
      chartsRef.current.push(dd);

      const sync = (a: IChartApi, b: IChartApi) =>
        a.timeScale().subscribeVisibleLogicalRangeChange((r) => r && b.timeScale().setVisibleLogicalRange(r));
      sync(equity, dd);
      sync(dd, equity);
    }

    const onResize = () => {
      chartsRef.current.forEach((c, i) => {
        const el = i === 0 ? equityRef.current : ddRef.current;
        if (el) c.applyOptions({ width: el.clientWidth });
      });
    };
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      chartsRef.current.forEach((c) => c.remove());
      chartsRef.current = [];
    };
  }, [result]);

  return (
    <>
      {m.account_blown && (
        <div className="notice error">
          <div className="notice-title">Account blown</div>
          Equity reached zero on {m.blown_at?.slice(0, 10)}. Risk-adjusted ratios are undefined past
          that point and are shown as “—” rather than as numbers, because percentage returns from a
          negative base are arithmetic noise, not performance.
        </div>
      )}

      {result.synthetic_data && (
        <div className="notice warn">
          <div className="notice-title">Synthetic data</div>
          These prices were generated, not observed. This validates that the strategy logic works;
          it is not evidence that the strategy makes money.
        </div>
      )}

      {result.warnings.map((w, i) => (
        <div className="notice warn" key={i}>
          <div className="notice-title">Preflight</div>
          {w}
        </div>
      ))}

      <MetricGrid m={m} />

      <div className="panel" style={{ marginTop: 12 }}>
        <div className="panel-title">Where the money went</div>
        <div className="metric-grid">
          <Cell label="Gross P&L" value={money(m.gross_pnl)} tone={m.gross_pnl >= 0 ? "good" : "bad"} />
          <Cell label="Fees paid" value={money(m.total_fees)} tone="bad"
            sub={`${result.costs.segment.replace("_", " ")}`} />
          <Cell label="Net P&L" value={money(m.net_pnl)} tone={m.net_pnl >= 0 ? "good" : "bad"} />
          <Cell
            label="Breakeven move"
            value={`${fmt(result.costs.breakeven_move)}`}
            sub={`${result.costs.breakeven_pct}% · round trip ${money(result.costs.round_trip.total)}`}
            tone="neutral"
          />
        </div>
        <div className="hint" style={{ marginTop: 8 }}>
          Every round trip must clear {fmt(result.costs.breakeven_move)} of movement before it earns
          anything. Brokerage, STT, exchange fees, SEBI turnover, GST and stamp duty are all included.
        </div>
      </div>

      {Object.keys(result.blocked_reasons).length > 0 && (
        <div className="panel" style={{ marginTop: 12 }}>
          <div className="panel-title">Entries refused by risk rules</div>
          <div className="chip-row">
            {Object.entries(result.blocked_reasons).map(([reason, count]) => (
              <span className="chip" key={reason}>{reason.replace(/_/g, " ")} × {count}</span>
            ))}
          </div>
          <div className="hint" style={{ marginTop: 6 }}>
            Signals that fired but were not taken. This is why the trade count may be lower than expected.
          </div>
        </div>
      )}

      {result.split && (
        <div className="panel" style={{ marginTop: 12 }}>
          <div className="panel-title">Out-of-sample check · split {result.split.split_date.slice(0, 10)}</div>
          <div className="table-wrap">
            <table style={{ minWidth: 0 }}>
              <thead>
                <tr><th>Window</th><th>Return</th><th>CAGR</th><th>Sharpe</th><th>Max DD</th><th>Trades</th></tr>
              </thead>
              <tbody>
                {([["In sample", result.split.in_sample], ["Out of sample", result.split.out_of_sample]] as const).map(
                  ([label, s]) => (
                    <tr key={label}>
                      <td>{label}</td>
                      <td className={`num ${s.total_return_pct >= 0 ? "pos" : "neg"}`}>{pct(s.total_return_pct)}</td>
                      <td className="num">{pct(s.cagr_pct)}</td>
                      <td className="num">{s.account_blown ? "—" : fmt(s.sharpe)}</td>
                      <td className="num">{fmt(s.max_drawdown_pct)}%</td>
                      <td className="num">{s.total_trades}</td>
                    </tr>
                  ),
                )}
              </tbody>
            </table>
          </div>
          <div className="hint" style={{ marginTop: 8 }}>
            <b>Verdict:</b> {result.split.verdict}. A strategy tuned until the in-sample numbers look
            good is a curve fit; the out-of-sample window is the only part that was not optimized.
          </div>
        </div>
      )}

      <div className="chart-box" style={{ marginTop: 12 }}>
        <div className="chart-head"><span>Equity curve</span><span>{money(m.starting_capital)} → {money(m.ending_equity)}</span></div>
        <div ref={equityRef} />
      </div>
      <div className="chart-box" style={{ marginTop: 12 }}>
        <div className="chart-head">
          <span>Drawdown %</span>
          <span>max {fmt(m.max_drawdown_pct)}% · longest {m.max_drawdown_days} days</span>
        </div>
        <div ref={ddRef} />
      </div>
    </>
  );
}

function MetricGrid({ m }: { m: Metrics }) {
  const undefinedIfBlown = (v: number) => (m.account_blown ? "—" : fmt(v));
  return (
    <div className="metric-grid">
      <Cell label="Total return" value={pct(m.total_return_pct)} tone={m.total_return_pct >= 0 ? "good" : "bad"}
        sub={`${money(m.starting_capital)} → ${money(m.ending_equity)}`} />
      <Cell label="CAGR" value={pct(m.cagr_pct)} tone={m.cagr_pct >= 0 ? "good" : "bad"} />
      <Cell label="Sharpe" value={undefinedIfBlown(m.sharpe)} tone={m.sharpe >= 1 ? "good" : m.sharpe < 0 ? "bad" : "neutral"}
        sub={`Sortino ${undefinedIfBlown(m.sortino)}`} />
      <Cell label="Max drawdown" value={`${fmt(m.max_drawdown_pct)}%`} tone="bad"
        sub={`${m.max_drawdown_days} days underwater`} />
      <Cell label="Trades" value={String(m.total_trades)} tone="neutral"
        sub={`${m.winners}W / ${m.losers}L · ${fmt(m.avg_bars_held, 0)} bars avg`} />
      <Cell label="Win rate" value={`${fmt(m.win_rate_pct, 1)}%`} tone={m.win_rate_pct >= 50 ? "good" : "neutral"}
        sub={`streak ${m.max_consecutive_wins}W / ${m.max_consecutive_losses}L`} />
      <Cell label="Profit factor" value={Number.isFinite(m.profit_factor) ? fmt(m.profit_factor) : "∞"}
        tone={m.profit_factor >= 1 ? "good" : "bad"} sub="gross win ÷ gross loss" />
      <Cell label="Expectancy" value={money(m.expectancy)} tone={m.expectancy >= 0 ? "good" : "bad"}
        sub={`avg win ${money(m.avg_win)} / loss ${money(m.avg_loss)}`} />
      <Cell label="Exposure" value={`${fmt(m.exposure_pct, 1)}%`} tone="neutral" sub="of bars in a position" />
      <Cell label="Volatility" value={m.account_blown ? "—" : `${fmt(m.volatility_pct, 1)}%`} tone="neutral"
        sub={`Calmar ${undefinedIfBlown(m.calmar)}`} />
    </div>
  );
}

function Cell({ label, value, sub, tone = "neutral" }: {
  label: string; value: string; sub?: string; tone?: "good" | "bad" | "neutral";
}) {
  return (
    <div className={`metric ${tone}`}>
      <div className="metric-label">{label}</div>
      <div className={`metric-value ${tone === "good" ? "pos" : tone === "bad" ? "neg" : ""}`}>{value}</div>
      {sub && <div className="metric-sub">{sub}</div>}
    </div>
  );
}

/* ================================================================== */
/* Trade log                                                           */
/* ================================================================== */

type SortKey = keyof Trade;

export function TradeLogTab({ result }: { result: BacktestResult }) {
  const [sortKey, setSortKey] = useState<SortKey>("entry_time");
  const [asc, setAsc] = useState(true);
  const [filter, setFilter] = useState<"all" | "wins" | "losses">("all");

  const rows = useMemo(() => {
    let data = [...result.trades];
    if (filter === "wins") data = data.filter((t) => t.net_pnl > 0);
    if (filter === "losses") data = data.filter((t) => t.net_pnl <= 0);

    data.sort((a, b) => {
      const x = a[sortKey], y = b[sortKey];
      const cmp = typeof x === "number" && typeof y === "number"
        ? x - y
        : String(x).localeCompare(String(y));
      return asc ? cmp : -cmp;
    });
    return data;
  }, [result.trades, sortKey, asc, filter]);

  const toggle = (key: SortKey) => {
    if (key === sortKey) setAsc(!asc);
    else { setSortKey(key); setAsc(true); }
  };

  const exportCsv = () => {
    const headers = ["entry_time", "exit_time", "symbol", "direction", "quantity", "entry_price",
      "exit_price", "gross_pnl", "fees", "net_pnl", "return_pct", "reason", "bars_held"];
    const lines = [headers.join(",")];
    for (const t of rows) lines.push(headers.map((h) => String((t as never)[h])).join(","));
    const url = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `trades_${result.strategy_id}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  if (!result.trades.length) {
    return (
      <div className="empty">
        <b>No trades</b>
        <span>The strategy produced no round trips over this data.</span>
        {Object.keys(result.blocked_reasons).length > 0 && (
          <span>Risk rules refused entries: {Object.keys(result.blocked_reasons).join(", ").replace(/_/g, " ")}</span>
        )}
        {result.warnings.length > 0 && <span>{result.warnings[0]}</span>}
      </div>
    );
  }

  const columns: [SortKey, string][] = [
    ["entry_time", "Entry"], ["exit_time", "Exit"], ["direction", "Side"],
    ["quantity", "Qty"], ["entry_price", "In"], ["exit_price", "Out"],
    ["gross_pnl", "Gross"], ["fees", "Fees"], ["net_pnl", "Net"],
    ["return_pct", "Return"], ["bars_held", "Bars"], ["reason", "Closed by"],
  ];

  return (
    <>
      <div style={{ display: "flex", gap: 8, marginBottom: 10, alignItems: "center" }}>
        <div className="chip-row">
          {(["all", "wins", "losses"] as const).map((f) => (
            <button key={f} className={`chip ${filter === f ? "active" : ""}`} onClick={() => setFilter(f)}>
              {f} {f === "all" ? result.trades.length : f === "wins" ? result.metrics.winners : result.metrics.losers}
            </button>
          ))}
        </div>
        <button className="btn secondary tiny" style={{ marginLeft: "auto" }} onClick={exportCsv}>
          Export CSV
        </button>
      </div>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              {columns.map(([key, label]) => (
                <th key={key} onClick={() => toggle(key)}>
                  {label}{sortKey === key ? (asc ? " ↑" : " ↓") : ""}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((t, i) => (
              <tr key={i}>
                <td>{t.entry_time.slice(0, 16).replace("T", " ")}</td>
                <td>{t.exit_time.slice(0, 16).replace("T", " ")}</td>
                <td><span className={`tag ${t.direction.toLowerCase()}`}>{t.direction}</span></td>
                <td className="num">{t.quantity}</td>
                <td className="num">{fmt(t.entry_price)}</td>
                <td className="num">{fmt(t.exit_price)}</td>
                <td className={`num ${t.gross_pnl >= 0 ? "pos" : "neg"}`}>{fmt(t.gross_pnl, 0)}</td>
                <td className="num neg">{fmt(t.fees, 0)}</td>
                <td className={`num ${t.net_pnl >= 0 ? "pos" : "neg"}`}><b>{fmt(t.net_pnl, 0)}</b></td>
                <td className={`num ${t.return_pct >= 0 ? "pos" : "neg"}`}>{pct(t.return_pct)}</td>
                <td className="num">{t.bars_held}</td>
                <td><span className="tag">{t.reason.replace(/_/g, " ")}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
