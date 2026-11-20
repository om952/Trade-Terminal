import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { LuckPanel } from "./Results";

afterEach(cleanup);

describe("LuckPanel", () => {
  it("sets the Sharpe beside what the best of the tries would reach by chance", () => {
    render(<LuckPanel d={{ sharpe: 1.42, days: 480, trials: 12, psr: 0.97, benchmark_sharpe: 1.81, dsr: 0.31,
      threshold: 0.95, verdict: "good on its own, not after the search" }} />);
    expect(screen.getByText("good on its own, not after the search")).toBeTruthy();
    expect(screen.getByText("from 480 daily returns")).toBeTruthy();
    expect(screen.getByText("Best of 12 by chance")).toBeTruthy();
    expect(screen.getByText("97%")).toBeTruthy();
    expect(screen.getByText("31%")).toBeTruthy();
    expect(screen.getByText(/95% is the bar/)).toBeTruthy();
  });

  it("says so when there is too little data", () => {
    render(<LuckPanel d={{ sharpe: 0.5, days: 2, trials: 1, psr: null, benchmark_sharpe: 0, dsr: null,
      threshold: 0.95, verdict: "too little data to judge" }} />);
    expect(screen.getByText("too little data to judge")).toBeTruthy();
    expect(screen.getAllByText("–").length).toBe(2);
  });
});

describe("HoldoutNotice", () => {
  it("says where the data was cut and offers the one final test", async () => {
    const { HoldoutNotice } = await import("./Results");
    let ran = 0;
    render(<HoldoutNotice holdout={{ start: "2026-05-01", final: false,
      note: "Data from 2026-05-01 is held out for one final test per idea, so this backtest ends on 2026-04-30." }}
      onFinalTest={() => { ran += 1; }} />);
    expect(screen.getByText("Held out from 2026-05-01")).toBeTruthy();
    screen.getByText("Run the final test").click();
    expect(ran).toBe(1);
  });

  it("marks a final test as the idea's one look", async () => {
    const { HoldoutNotice } = await import("./Results");
    render(<HoldoutNotice holdout={{ start: "2026-05-01", final: true, note: "Final test." }} />);
    expect(screen.getByText("Final test on held-out data")).toBeTruthy();
    expect(screen.getByText(/do not retune and test again/)).toBeTruthy();
    expect(screen.queryByText("Run the final test")).toBeNull();
  });

  it("shows nothing when the run ended before the holdout anyway", async () => {
    const { HoldoutNotice } = await import("./Results");
    const { container } = render(<HoldoutNotice holdout={{ start: "2026-05-01", final: false, note: null }} />);
    expect(container.textContent).toBe("");
  });
});
