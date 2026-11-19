import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { numericPaths, parseValues, pathLabel, SweepEditor, SweepTable, sweepCount } from "./Sweep";
import type { SweepResult } from "./api";

afterEach(cleanup);

const doc = {
  name: "EMA",
  schema_version: 2,
  indicators: [
    { id: "ema_fast", type: "EMA", params: { period: 9, source: "close" } },
    { id: "ema_slow", type: "EMA", params: { period: 21, source: "close" } },
  ],
  legs: [{ id: "leg1", action: "BUY", kind: "equity", lots: 1, stop_loss: { type: "percent", value: 1.5 } }],
  sweep: [{ path: "/indicators/0/params/period", values: [5, 9] }],
};

describe("sweep helpers", () => {
  it("parses lists and ranges, and rejects nonsense", () => {
    expect(parseValues("5, 9, 13")).toEqual([5, 9, 13]);
    expect(parseValues("5..20 step 5")).toEqual([5, 10, 15, 20]);
    expect(parseValues("0.5..1.5 step 0.5")).toEqual([0.5, 1, 1.5]);
    expect(typeof parseValues("five")).toBe("string");
    expect(typeof parseValues("20..5 step 5")).toBe("string");
    expect(typeof parseValues("1..1000 step 1")).toBe("string");
  });

  it("lists every number but the sweep and the version, labelled by id", () => {
    const paths = numericPaths(doc);
    expect(paths).toEqual([
      "/indicators/0/params/period", "/indicators/1/params/period",
      "/legs/0/lots", "/legs/0/stop_loss/value",
    ]);
    expect(paths.map((p) => pathLabel(p, doc))).toEqual([
      "ema_fast.period", "ema_slow.period", "leg1.lots", "leg1.stop_loss.value",
    ]);
  });

  it("counts combinations", () => {
    expect(sweepCount({})).toBe(0);
    expect(sweepCount(doc)).toBe(2);
    expect(sweepCount({ sweep: [{ values: [1, 2, 3] }, { values: [1, 2] }] })).toBe(6);
  });
});

function Harness({ onDoc }: { onDoc: (d: Record<string, any>) => void }) {
  const [d, setD] = useState<Record<string, any>>(doc);
  return <SweepEditor doc={d} setDoc={(next) => { setD(next); onDoc(next); }} />;
}

describe("SweepEditor", () => {
  it("edits values, adds and removes parameters", () => {
    const onDoc = vi.fn();
    render(<Harness onDoc={onDoc} />);
    expect(screen.getByText("2 backtests")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Values for ema_fast.period"), { target: { value: "5..15 step 5" } });
    expect(onDoc.mock.lastCall![0].sweep[0].values).toEqual([5, 10, 15]);

    fireEvent.change(screen.getByLabelText("Add a parameter to sweep"), { target: { value: "/legs/0/stop_loss/value" } });
    const added = onDoc.mock.lastCall![0].sweep[1];
    expect(added.path).toBe("/legs/0/stop_loss/value");
    expect(added.values[0]).toBe(1.5);
    expect(screen.getByText("9 backtests")).toBeTruthy();

    // A typo is flagged and never reaches the document.
    const calls = onDoc.mock.calls.length;
    fireEvent.change(screen.getByLabelText("Values for leg1.stop_loss.value"), { target: { value: "1, x" } });
    expect(onDoc.mock.calls.length).toBe(calls);
    expect(screen.getByText(/List numbers separated by commas/)).toBeTruthy();

    for (const b of screen.getAllByTitle("Stop varying this")) fireEvent.click(b);
    expect("sweep" in onDoc.mock.lastCall![0]).toBe(false);
  });
});

const result: SweepResult = {
  family: "fam", family_trials: { runs: 7, variants: 5 },
  axes: [{ path: "/indicators/0/params/period", label: "ema_fast.period", values: [5, 9] }],
  symbol: "SIM|NIFTY", timeframe: "15m", synthetic_data: true, data: null,
  variants: [5, 9].map((p, i) => ({
    label: `ema_fast.period=${p}`, assignment: { "/indicators/0/params/period": p },
    run_id: `run_${i}`, strategy_hash: `h${i}`,
    metrics: { total_trades: 40 + i, net_pnl: i ? 5000 : -2000, total_return_pct: i ? 0.5 : -0.2,
      sharpe: 0.4, max_drawdown_pct: -3, win_rate_pct: 50, profit_factor: 1.1 },
  })),
  skipped: [{ label: "ema_fast.period=0", assignment: {}, reason: "period: must be at least 1" }],
  note: "Every variant is recorded as a trial of this strategy's family.",
};

describe("SweepTable", () => {
  it("shows every variant in grid order with the spread and trial count, and opens reports", () => {
    const open = vi.fn();
    const use = vi.fn();
    render(<SweepTable sweep={result} onOpenRun={open} onUseRun={use} />);
    const rows = screen.getAllByRole("row").slice(1);
    expect(rows.map((r) => r.querySelector("td")!.textContent)).toEqual(["5", "9"]);
    expect(screen.getByText(/from -2,000 to 5,000 · 1 of 2 made money · 7 backtests/)).toBeTruthy();
    expect(screen.getByText("synthetic data")).toBeTruthy();
    expect(screen.getByText(/1 skipped as not valid/)).toBeTruthy();
    expect(screen.queryByText(/best variant|winner/i)).toBeNull();

    fireEvent.click(screen.getAllByText("Report")[1]);
    expect(open).toHaveBeenCalledWith("run_1");
    fireEvent.click(screen.getAllByText("Use")[0]);
    expect(use).toHaveBeenCalledWith("run_0");
  });
});
