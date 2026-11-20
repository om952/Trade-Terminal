import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { PortfolioView, type PortfolioRun } from "./Portfolio";

afterEach(cleanup);

const run: PortfolioRun = {
  name: "Momentum rotation", synthetic_data: true,
  data: { instruments: 4, benchmark: "SIM|NIFTY", first_bar: "2024-01-01T09:15:00+05:30", last_bar: "2025-06-30T09:15:00+05:30",
    missing: [{ symbol: "SIM|GONE", reason: "no daily bars" }] },
  metrics: { net_pnl: 125000, total_return_pct: 12.5, cagr_pct: 8.1, max_drawdown_pct: 6.2, sharpe: 0.9, total_trades: 14 },
  trades: [{ symbol: "SIM|A", entry_time: "2024-04-02", exit_time: "2024-09-02", quantity: 50, entry_price: 100,
    exit_price: 120, net_pnl: 980, reason: "rebalance" }],
  equity_curve: [{ time: 1, equity: 1_000_000, drawdown: 0 }, { time: 2, equity: 1_125_000, drawdown: 0 }],
  rebalances: [
    { date: "2024-04-01", regime_on: true, eligible: 3, targets: ["SIM|A", "SIM|B"], orders: 2 },
    { date: "2024-05-01", regime_on: false, eligible: 3, targets: [], orders: 2 },
  ],
  holdings: [],
  notes: ["Synthetic data: this result says nothing about real markets."],
};

describe("PortfolioView", () => {
  it("shows results, holdings, every rebalance and what could not be loaded", () => {
    render(<PortfolioView job={null} run={run} />);
    expect(screen.getByText(/4 stocks · 2024-01-01 to 2025-06-30 · filter on SIM\|NIFTY/)).toBeTruthy();
    expect(screen.getByLabelText("Portfolio results").textContent).toContain("12.50%");
    expect(screen.getByText("In cash.")).toBeTruthy();
    const rows = screen.getByLabelText("Rebalances").querySelectorAll("tbody tr");
    expect(rows[0].textContent).toContain("2024-05-01");      // newest first
    expect(rows[0].textContent).toContain("cash");
    expect(rows[1].textContent).toContain("SIM|A, SIM|B");
    expect(screen.getByLabelText("Portfolio trades").textContent).toContain("rebalance");
    expect(screen.getByText("1 not loaded")).toBeTruthy();
    expect(screen.getByLabelText("Equity curve")).toBeTruthy();
  });

  it("shows progress and failures", () => {
    const job = { id: "j", kind: "portfolio", status: "running" as const, progress: 0.3,
      message: "Loading 2 of 5: SIM|B", result: null, error: null };
    render(<PortfolioView job={job} run={null} />);
    expect(screen.getByText("Loading 2 of 5: SIM|B")).toBeTruthy();
  });
});
