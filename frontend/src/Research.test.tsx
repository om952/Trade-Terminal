import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ResearchView } from "./Research";
import type { ResearchSession } from "./api";

afterEach(cleanup);

const base: ResearchSession = {
  id: "res_1", title: "Fewer false signals", instrument: "SIM|NIFTY", phase: "awaiting_plan_approval",
  goal: "filters", regime: "flat, low volatility", ranking: [], kept: [], events: [], error: null, job: null,
  proposals: [
    { id: "v1", hypothesis: "RSI filter", change_summary: "only long above RSI 50", status: "accepted" },
    { id: "v2", hypothesis: "Trend filter", change_summary: "only above the 200-day average", status: "accepted" },
    { id: "v3", hypothesis: "Faster EMA", change_summary: "EMA 5", status: "rejected",
      reason: "only changes numbers; that is a sweep, not a new idea" },
  ],
  estimate: { candidates: 3, backtests_min: 3, backtests_max: 24, max_backtests: 120, model_calls_used: 1,
    model_tokens_used: 1200, model_calls_to_come: 0 },
};

describe("ResearchView", () => {
  it("shows the plan and its cost, and approves with the variants the trader dropped", () => {
    const onReply = vi.fn();
    render(<ResearchView session={base} onReply={onReply} onOpenRun={() => {}} onOpenStrategy={() => {}} />);
    expect(screen.getByText(/only changes numbers/)).toBeTruthy();
    expect(screen.getByLabelText("Cost").textContent).toContain("3 candidates");
    fireEvent.click(screen.getByLabelText("Run Trend filter"));
    expect(screen.getByLabelText("Cost").textContent).toContain("2 candidates");
    expect(screen.getByLabelText("Cost").textContent).toContain("up to 16");
    fireEvent.click(screen.getByText("Approve and run"));
    expect(onReply).toHaveBeenCalledWith({ action: "approve", drop: ["v2"] });
  });

  it("lists every candidate with its deflated Sharpe and keeps only chosen survivors", () => {
    const onReply = vi.fn();
    const metrics = { sharpe: 1.3, net_pnl: 5000, total_trades: 40, max_drawdown_pct: 3 };
    const session: ResearchSession = {
      ...base, phase: "awaiting_choice",
      events: [{ kind: "judged", text: "1 of 2 survived after 3 tries", data: { notes: [] } }],
      ranking: [
        { id: "v1", hypothesis: "RSI filter", family: "f1", run_id: "run_a", metrics, dsr: 0.97, outcome: "survived",
          reason: "clears the deflated Sharpe bar", checks: { profitable_folds: 4, folds: 4, survives_slippage: true, verdicts: [] } },
        { id: "base", hypothesis: "the base strategy", family: "f0", run_id: "run_b", metrics: { ...metrics, sharpe: 0.2 },
          dsr: 0.31, outcome: "failed", reason: "could be luck after 3 tries (deflated Sharpe 31%)" },
      ],
    };
    const onOpenRun = vi.fn();
    render(<ResearchView session={session} onReply={onReply} onOpenRun={onOpenRun} onOpenStrategy={() => {}} />);
    expect(screen.getByText(/1 of 2 survived after 3 tries/)).toBeTruthy();
    expect(screen.getByText("97%")).toBeTruthy();
    expect(screen.getByText("4/4 folds · 2x slippage ok")).toBeTruthy();
    expect(screen.queryByLabelText("Keep the base strategy")).toBeNull();
    fireEvent.click(screen.getByLabelText("Keep RSI filter"));
    fireEvent.click(screen.getByText("Keep 1 in the library"));
    expect(onReply).toHaveBeenCalledWith({ keep: ["v1"] });
    fireEvent.click(screen.getAllByText("Report")[1]);
    expect(onOpenRun).toHaveBeenCalledWith("run_b");
  });

  it("says so when nothing survived", () => {
    const session: ResearchSession = {
      ...base, phase: "awaiting_choice",
      ranking: [{ id: "base", hypothesis: "the base strategy", family: "f0", run_id: "r", metrics: null, dsr: 0.1,
        outcome: "failed", reason: "could be luck" }],
    };
    const onReply = vi.fn();
    render(<ResearchView session={session} onReply={onReply} onOpenRun={() => {}} onOpenStrategy={() => {}} />);
    expect(screen.getByText(/No candidate survived/)).toBeTruthy();
    fireEvent.click(screen.getByText("Finish"));
    expect(onReply).toHaveBeenCalledWith({ keep: [] });
  });
});
