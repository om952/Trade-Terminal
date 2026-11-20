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
