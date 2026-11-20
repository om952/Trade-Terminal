import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { HistoryPrefetch, ScanAssistant, ScanResults } from "./Screener";
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

describe("HistoryPrefetch", () => {
  const plan = {
    timeframe: "1d", start: "2021-11-19", end: "2026-11-19", instruments: 3, to_download: 2, up_to_date: 1,
    skipped: 0, missing_sessions: 2480, calls: 3000, eta_seconds: 1800, binding_limit: "1900 requests per 30 min",
    broker_ready: false, broker_reason: "no access token", rows: [],
  };

  function stub(routes: Record<string, (init?: RequestInit) => unknown>) {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const key = `${init?.method ?? "GET"} ${url}`;
      calls.push(key);
      const route = Object.keys(routes).find((k) => key.startsWith(k));
      return new Response(JSON.stringify(route ? routes[route](init) : { detail: "unmocked" }), { status: route ? 200 : 500 });
    }));
    return calls;
  }

  afterEach(() => vi.unstubAllGlobals());

  it("shows what is missing, the requests and the time, and refuses without a broker", async () => {
    stub({ "POST /api/data/prefetch/plan": () => plan });
    render(<HistoryPrefetch universe={{ symbols: ["A", "B", "C"] }} enabled />);
    fireEvent.click(screen.getByText("Check history"));
    const status = await screen.findByLabelText("History plan");
    expect(status.textContent).toContain("1 of 3 up to date");
    expect(status.textContent).toContain("2,480 sessions in 3,000 requests, about 30 min (Upstox allows 1900 requests per 30 min)");
    expect(status.textContent).toContain("Upstox is not connected: no access token");
    expect((screen.getByText("Download") as HTMLButtonElement).disabled).toBe(true);
  });

  it("downloads with progress, and offers to resume once stopped", async () => {
    const job = (status: string, extra = {}) => ({ id: "j1", kind: "prefetch", status, progress: 0.4,
      message: "2 of 3: B · 1200/3000 requests · about 18 min left", result: null, error: null, ...extra });
    const calls = stub({
      "POST /api/data/prefetch/plan": () => ({ ...plan, broker_ready: true, broker_reason: null }),
      "POST /api/jobs/prefetch": () => job("running"),
      "GET /api/jobs/j1": () => job("cancelled", { error: "Cancelled" }),
      "POST /api/jobs/j1/resume": () => ({ ...job("running"), id: "j2" }),
      "GET /api/jobs/j2": () => job("running", { id: "j2" }),
    });
    render(<HistoryPrefetch universe={{ preset: "fno_stocks" }} enabled />);
    fireEvent.click(screen.getByText("Check history"));
    fireEvent.click(await screen.findByText("Download"));
    expect(await screen.findByText(/1200\/3000 requests · about 18 min left/)).toBeTruthy();
    expect(await screen.findByText(/Resuming skips what is already stored/, {}, { timeout: 3000 })).toBeTruthy();
    fireEvent.click(await screen.findByText("Resume download"));
    await screen.findByText(/1200\/3000 requests/);
    expect(calls).toContain("POST /api/jobs/j1/resume");
  });
});

describe("ScanAssistant", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("asks, reads back, and hands the spec over only when the trader chooses", async () => {
    const spec = { name: "Breakout", universe: { preset: "fno_stocks" }, condition: { op: "AND", conditions: [] } };
    const bodies: Record<string, unknown>[] = [];
    let n = 0;
    const results = [
      { status: "needs_clarification", dialogue: [{ role: "user", content: "breakouts" }],
        questions: [{ question: "Which line?", why: "it decides the trigger", options: ["20-day high", "50-day high"] }] },
      { status: "ready", spec, assumptions: ["Stocks with NSE futures"],
        card: { title: "Breakout", summary: "", sections: [{ heading: "Find where", lines: ["close > the 100th percentile of high"] }] } },
    ];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        bodies.push(JSON.parse(init.body as string));
        return new Response(JSON.stringify({ id: `j${++n}`, kind: "scan_compile", status: "queued", progress: 0, message: "", result: null, error: null }));
      }
      const id = Number(url.split("/j").pop());
      return new Response(JSON.stringify({ id: `j${id}`, kind: "scan_compile", status: "succeeded", progress: 1, message: "", result: results[id - 1], error: null }));
    }));
    const onUse = vi.fn();
    render(<ScanAssistant onUse={onUse} />);
    fireEvent.change(screen.getByLabelText("Describe a scan"), { target: { value: "breakouts" } });
    fireEvent.click(screen.getByText("Build scan"));

    fireEvent.click(await screen.findByText("20-day high", {}, { timeout: 3000 }));
    fireEvent.click(screen.getByText("Answer"));
    expect(await screen.findByText(/Check before scanning: Breakout/, {}, { timeout: 3000 })).toBeTruthy();
    expect(bodies[1]).toEqual({ text: "Q: Which line?\nA: 20-day high", dialogue: [{ role: "user", content: "breakouts" }] });
    expect(onUse).not.toHaveBeenCalled();

    fireEvent.click(screen.getByText("Use and scan now"));
    expect(onUse).toHaveBeenCalledWith(spec, true);
  });
});
