import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { api, type PortfolioSessionDetail, type PortfolioSessionSummary } from "./api";
import { PaperPortfolios } from "./PaperPortfolio";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

const summary: PortfolioSessionSummary = {
  id: "pfs_1", name: "Rotation", mode: "paper", status: "active", status_reason: null, start_date: "2026-11-20",
  starting_capital: 1_000_000, slippage_bps: 5, members: 4, benchmark: null, last_bar_date: "2026-11-24",
  last_step_at: "2026-11-25T08:50:00+05:30", created_at: "2026-11-20T10:00:00+05:30", stopped_at: null,
  equity: 1_010_000, net_pnl: 10_000, return_pct: 1,
};

const detail: PortfolioSessionDetail = {
  ...summary, spec: {},
  state: {
    as_of: "2026-11-24", equity: 1_010_000, cash: 20_000, net_pnl: 10_000, return_pct: 1, realized_pnl: 0,
    unrealized_pnl: 10_000, max_drawdown_pct: -0.5,
    holdings: [{ symbol: "SIM|A", quantity: 50, avg_price: 100, last_close: 104, since: "2026-11-23" }],
    pending: [{ symbol: "SIM|B", side: "SELL", quantity: 30, reason: "rebalance" },
              { symbol: "SIM|C", side: "BUY", quantity: 40, reason: "entry_signal" }],
    trades: [], missing: [], notes: [],
  },
  days: [
    { day: "2026-11-20", equity: 1_000_000, cash: 1_000_000, fills: [], rebalance: { regime_on: true, targets: ["SIM|A"], orders: 1 } },
    { day: "2026-11-23", equity: 1_004_000, cash: 20_000, fills: [{ symbol: "SIM|A", side: "BUY", quantity: 50, price: 100, fees: 12, reason: "entry_signal" }], rebalance: null },
    { day: "2026-11-24", equity: 1_010_000, cash: 20_000, fills: [], rebalance: null },
  ],
};

describe("PaperPortfolios", () => {
  it("lists sessions and shows the open session's next orders, holdings and daily record", async () => {
    vi.spyOn(api, "portfolioSessions").mockResolvedValue({ sessions: [summary], step_times: "08:50" });
    vi.spyOn(api, "portfolioSession").mockResolvedValue(detail);
    render(<PaperPortfolios focus="pfs_1" setFocus={() => undefined} version={0} onError={() => undefined} />);
    await waitFor(() => expect(screen.getByLabelText("Orders for the next open")).toBeTruthy());
    expect(screen.getByText(/at 08:50 IST/)).toBeTruthy();
    const orders = screen.getByLabelText("Orders for the next open").querySelectorAll("tbody tr");
    expect(orders[0].textContent).toContain("SELL");     // sells first, as they fill
    expect(orders[1].textContent).toContain("SIM|C");
    expect(screen.getByLabelText("Paper holdings").textContent).toContain("SIM|A");
    const record = screen.getByLabelText("Daily record").querySelectorAll("tbody tr");
    expect(record[0].textContent).toContain("2026-11-24");             // newest first
    expect(record[1].textContent).toContain("BUY 50 SIM|A @ 100.00");
    expect(screen.getByRole("button", { name: "Update now" })).toBeTruthy();
  });

  it("says when it waits for its first close, and shows why a session halted", async () => {
    const halted = { ...detail, status: "halted" as const, state: null, days: [],
      status_reason: "Halted: the price history changed under the paper record." };
    vi.spyOn(api, "portfolioSessions").mockResolvedValue({ sessions: [{ ...summary, status: "halted" }], step_times: "08:50" });
    vi.spyOn(api, "portfolioSession").mockResolvedValue(halted);
    render(<PaperPortfolios focus="pfs_1" setFocus={() => undefined} version={0} onError={() => undefined} />);
    await waitFor(() => expect(screen.getByText(/price history changed/)).toBeTruthy());
    expect(screen.getByText(/Waiting for the first session close on or after 2026-11-20/)).toBeTruthy();
    expect((screen.getByRole("button", { name: "Update now" }) as HTMLButtonElement).disabled).toBe(true);
  });
});
