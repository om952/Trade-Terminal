import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Assistant } from "./Assistant";
import type { Conversation, RunSummary } from "./api";

const configured = {
  provider: "anthropic", model: "claude-opus-5-5", effort: "high", key_source: "keychain",
  configured: true, limits: { tokens_per_compile: 200000, model_calls_per_compile: 8, seconds_per_compile: 300 },
};

const metrics = {
  starting_capital: 1_000_000, net_pnl: 12_345, total_return_pct: 1.23, max_drawdown_pct: -2.5,
  win_rate_pct: 55.5, sharpe: 1.1, total_trades: 9,
};

function conversation(over: Partial<Conversation>): Conversation {
  return {
    id: "conv_1", title: "EMA", created_at: "", updated_at: "", strategy_id: null, version: null,
    phase: "awaiting_approval", pending: null, transcript: [], document: { name: "EMA" },
    assumptions: [], run: null, error: null,
    usage: { steps: 1, total: 1500, seconds: 3, limits: { steps: 8, tokens: 200000, seconds: 300 } },
    job: null, can_retry: false, ...over,
  } as Conversation;
}

type Route = (url: string, init?: RequestInit) => unknown;

function mockFetch(routes: Record<string, Route>) {
  const calls: { url: string; init?: RequestInit }[] = [];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    const key = Object.keys(routes).find((k) => {
      const [method, path] = k.split(" ");
      return (init?.method ?? "GET") === method && url.startsWith(path);
    });
    if (!key) return new Response(JSON.stringify({ detail: `unmocked ${url}` }), { status: 500 });
    return new Response(JSON.stringify(routes[key](url, init)), { status: 200 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

const noop = () => {};
const props = { onOpenRun: noop, onStartPaper: noop, onOpenDocument: noop };

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function openConversation(conv: Conversation, extra: Record<string, Route> = {}) {
  const calls = mockFetch({
    "GET /api/llm/status": () => configured,
    "GET /api/agent/conversations/conv_1": () => conv,
    "GET /api/agent/conversations": () => ({ conversations: [{ id: "conv_1", title: "EMA" }] }),
    ...extra,
  });
  const view = render(<Assistant {...props} />);
  const select = await screen.findByRole("combobox");
  await waitFor(() => expect(screen.getByRole("option", { name: "EMA" })).toBeTruthy());
  fireEvent.change(select, { target: { value: "conv_1" } });
  return { calls, view };
}

describe("Assistant", () => {
  it("asks for a key when no model is configured, and stores it", async () => {
    const calls = mockFetch({
      "GET /api/llm/status": () => ({ ...configured, configured: false, key_source: null }),
      "GET /api/agent/conversations": () => ({ conversations: [] }),
      "PUT /api/llm/key": () => configured,
    });
    render(<Assistant {...props} />);
    const input = await screen.findByLabelText("Anthropic API key");
    fireEvent.change(input, { target: { value: "sk-ant-api03-xxxxxxxxxxxxxxxxxxxx" } });
    fireEvent.click(screen.getByText("Save key"));
    await screen.findByText("Strategy assistant");
    const put = calls.find((c) => c.init?.method === "PUT")!;
    expect(JSON.parse(put.init!.body as string)).toEqual({
      provider: "anthropic", api_key: "sk-ant-api03-xxxxxxxxxxxxxxxxxxxx",
    });
  });

  it("shows the read-back and assumptions, and approves", async () => {
    const pending = {
      type: "approval" as const,
      card: { title: "EMA cross", summary: "", sections: [{ heading: "Enter when", lines: ["ema9 crosses above ema21"] }] },
      assumptions: ["15-minute bars"],
      document: { name: "EMA cross" },
    };
    const { calls } = await openConversation(conversation({ pending }), {
      "POST /api/agent/conversations/conv_1/reply": () => conversation({ phase: "working", job: { id: "j", status: "running", progress: 0.2, message: "Running the backtest", error: null } }),
    });
    await screen.findByText("ema9 crosses above ema21");
    expect(screen.getByText("15-minute bars")).toBeTruthy();
    expect(screen.getByText(/Assumed/)).toBeTruthy();

    fireEvent.click(screen.getByText("Approve and backtest"));
    await screen.findByText("Running the backtest");
    const reply = calls.find((c) => c.url.endsWith("/reply"))!;
    expect(JSON.parse(reply.init!.body as string)).toEqual({ action: "approve" });
  });

  it("says how many backtests approving a sweep runs, and shows the grid", async () => {
    const pending = {
      type: "approval" as const,
      card: { title: "EMA cross", summary: "", sections: [{ heading: "Sweep", lines: ["ema_fast.period: 5, 9, 13"] }] },
      assumptions: [],
      document: { name: "EMA cross", sweep: [{ path: "/indicators/0/params/period", values: [5, 9, 13] }] },
    };
    await openConversation(conversation({ pending }));
    expect(await screen.findByText("Approve and run 3 backtests")).toBeTruthy();
    cleanup();
    vi.unstubAllGlobals();

    const sweep = {
      family: "f", family_trials: { runs: 3, variants: 3 },
      axes: [{ path: "/indicators/0/params/period", label: "ema_fast.period", values: [5, 9] }],
      symbol: "SIM|AGENT", timeframe: "15m", synthetic_data: false, data: null, skipped: [],
      note: "Every variant is recorded as a trial.",
      variants: [5, 9].map((p) => ({
        label: `ema_fast.period=${p}`, assignment: { "/indicators/0/params/period": p }, run_id: `r${p}`,
        strategy_hash: "h", metrics: { ...metrics, profit_factor: 1 },
      })),
    };
    await openConversation(conversation({
      phase: "awaiting_followup",
      transcript: [{ role: "system", kind: "sweep", text: "", data: { sweep } }],
    }));
    expect(await screen.findByText(/Sweep · SIM\|AGENT 15m · 2 variants/)).toBeTruthy();
    expect(screen.getAllByText("Report")).toHaveLength(2);
  });

  it("sends a change request instead of approving", async () => {
    const pending = {
      type: "approval" as const,
      card: { title: "EMA cross", summary: "", sections: [] },
      assumptions: [], document: {},
    };
    const { calls } = await openConversation(conversation({ pending }), {
      "POST /api/agent/conversations/conv_1/reply": () => conversation({ phase: "working", job: { id: "j", status: "running", progress: 0, message: "Applying the change", error: null } }),
    });
    fireEvent.click(await screen.findByText("Change something"));
    fireEvent.change(screen.getByLabelText("Requested change"), { target: { value: "add a 1% stop" } });
    fireEvent.click(screen.getByText("Send change"));
    await screen.findByText("Applying the change");
    const reply = calls.find((c) => c.url.endsWith("/reply"))!;
    expect(JSON.parse(reply.init!.body as string)).toEqual({ action: "change", text: "add a 1% stop" });
  });

  it("answers clarifying questions from the offered options", async () => {
    const pending = {
      type: "questions" as const,
      questions: [{ question: "Which index?", why: "No instrument named", options: ["NIFTY", "BANKNIFTY"] }],
    };
    const { calls } = await openConversation(conversation({ phase: "awaiting_answers", pending }), {
      "POST /api/agent/conversations/conv_1/reply": () => conversation({ phase: "working", job: { id: "j", status: "running", progress: 0, message: "Reading the strategy", error: null } }),
    });
    fireEvent.click(await screen.findByText("BANKNIFTY"));
    fireEvent.click(screen.getByText("Answer"));
    await screen.findByText("Reading the strategy");
    const reply = calls.find((c) => c.url.endsWith("/reply"))!;
    expect(JSON.parse(reply.init!.body as string)).toEqual({ answers: ["BANKNIFTY"] });
  });

  it("shows a result with its data provenance and starts paper trading", async () => {
    const run = {
      run_id: "run_1", strategy_hash: "h", symbol: "SIM|NIFTY", timeframe: "15m", synthetic_data: true,
      metrics, warnings: ["Only 9 trades"], blocked_reasons: {}, trades: 9, trials: 2,
      family: "fam_1", family_trials: { runs: 7, variants: 4 },
      data: { instrument_key: "SIM|NIFTY", timeframe: "15m", source: "simulated", derived_from: null,
              synthetic: true, first_bar: "2025-01-01T09:15:00+05:30", last_bar: "2025-06-30T15:15:00+05:30",
              bars: 3000, fingerprint: "abcdef1234567890", missing_sessions: 0, fetched_sessions: 0, notes: [] },
    } as unknown as RunSummary;
    const onStartPaper = vi.fn();
    const onOpenRun = vi.fn();
    mockFetch({
      "GET /api/llm/status": () => configured,
      "GET /api/agent/conversations/conv_1": () => conversation({
        phase: "awaiting_followup", pending: { type: "followup" }, run,
        transcript: [{ role: "system", kind: "result", text: "", data: { run } }],
      }),
      "GET /api/agent/conversations": () => ({ conversations: [{ id: "conv_1", title: "EMA" }] }),
    });
    render(<Assistant onOpenRun={onOpenRun} onStartPaper={onStartPaper} onOpenDocument={noop} />);
    await waitFor(() => expect(screen.getByRole("option", { name: "EMA" })).toBeTruthy());
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "conv_1" } });

    await screen.findByText("synthetic data");
    expect(screen.getByText("12,345")).toBeTruthy();
    expect(screen.getByText(/backtest #2 of this exact strategy/)).toBeTruthy();
    expect(screen.getByText(/7 backtests across 4 variants of this idea/)).toBeTruthy();
    expect(screen.getByText("Only 9 trades")).toBeTruthy();
    expect(screen.getByLabelText("Ask for a change")).toBeTruthy();

    fireEvent.click(screen.getByText("Open full report"));
    expect(onOpenRun).toHaveBeenCalledWith("run_1");
    fireEvent.click(screen.getByText("Start paper trading"));
    expect(onStartPaper).toHaveBeenCalledWith({ name: "EMA" }, run);
  });

  it("offers to continue a turn that was cut off", async () => {
    const { calls } = await openConversation(
      conversation({ phase: "interrupted", can_retry: true, job: { id: "j", status: "failed", progress: 0.3, message: "", error: "process died" } }),
      { "POST /api/agent/conversations/conv_1/retry": () => conversation({ phase: "working", job: { id: "j2", status: "queued", progress: 0, message: "", error: null } }) },
    );
    await screen.findByText("process died");
    fireEvent.click(screen.getByText("Continue where it stopped"));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/retry"))).toBe(true));
  });
});
