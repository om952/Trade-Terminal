import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ScanResults } from "./Screener";
import type { Job, ScanResult } from "./api";

afterEach(cleanup);

const base = { spec_hash: "h", name: "Breakout", side: "long" as const, notes: ["Some instruments are synthetic data."] };

const now: ScanResult = {
  ...base, mode: "now", universe: 4, as_of: "2026-11-19",
  counts: { triggered_today: 1, lapsed: 1, waiting: 1, no_data: 1 },
  rows: [
    { symbol: "TCS", key: "k1", status: "triggered_today", close: 2335.4, distance_pct: 2.0, since: "2026-11-19", since_close: 2335.4, stale: false },
    { symbol: "HCLTECH", key: "k2", status: "lapsed", close: 1310.7, distance_pct: -0.4, last_fired: "2026-11-17", last_fired_close: 1355.7, bars_since: 2, stale: true, as_of: "2026-11-17" },
    { symbol: "TATAELXSI", key: "k3", status: "waiting", close: 3682.5, distance_pct: -1.1, stale: false },
    { symbol: "NOPE", key: "k4", status: "no_data", reason: "No daily bars stored." },
  ],
};

describe("ScanResults", () => {
  it("shows status, when it fired, and the distance to the line, and lists what was not scanned", () => {
    render(<ScanResults job={null} result={now} onCancel={() => {}} />);
    const rows = screen.getAllByRole("row").slice(1);
    expect(rows).toHaveLength(3);
    expect(rows[0].textContent).toContain("Triggered today");
    expect(rows[0].textContent).toContain("+2.0% beyond");
    expect(rows[1].textContent).toContain("2026-11-17");
    expect(rows[1].textContent).toContain("2 bars ago · closed 1,355.70");
    expect(rows[1].textContent).toContain("stale");
    expect(rows[2].textContent).toContain("1.1% to go");
    expect(screen.getByText("1 not scanned")).toBeTruthy();
    expect(screen.getByText(/No daily bars stored/)).toBeTruthy();
    expect(screen.getAllByText("▲ Long")).toHaveLength(3);
  });

  it("sets every history figure next to its baseline", () => {
    const stats = (count: number, mean: number) => ({ count, mean, median: mean, hit_rate: 55 });
    const history: ScanResult = {
      ...base, mode: "history", universe: 1, counts: { ok: 1 }, rows: [],
      summary: { "1": { signal: stats(10, 0.3), baseline: stats(500, 0.1) },
                 "5": { signal: stats(9, 1.2), baseline: stats(496, 0.5) },
                 "20": { signal: stats(8, 2.5), baseline: stats(480, 1.9) } },
      events: [{ symbol: "TCS", date: "2026-10-01", close: 100, forward: { "1": 0.5, "5": -1, "20": null } }],
    };
    render(<ScanResults job={null} result={history} onCancel={() => {}} />);
    expect(screen.getByText("of 500 bars")).toBeTruthy();
    expect(screen.getAllByText(/baseline 0\.10%/).length).toBeGreaterThan(0);
    expect(screen.getByText("-1.00%")).toBeTruthy();
  });

  it("shows progress and cancels a running scan", () => {
    const onCancel = vi.fn();
    const job = { id: "j", kind: "scan", status: "running", progress: 0.4, message: "12 of 30: TCS", result: null, error: null } as Job<ScanResult>;
    render(<ScanResults job={job} result={null} onCancel={onCancel} />);
    expect(screen.getByText("12 of 30: TCS")).toBeTruthy();
    fireEvent.click(screen.getByText("Cancel"));
    expect(onCancel).toHaveBeenCalled();
  });
});
