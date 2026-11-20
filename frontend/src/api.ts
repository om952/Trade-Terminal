// Typed client for the backend. Every trading calculation lives on the server;
// this file only moves JSON. Anything computed here could drift from what a
// backtest or a live session does, which is exactly what the one-engine design
// exists to prevent.

export interface IndicatorMeta {
  name: string;
  category: string;
  params: Record<string, number | string>;
  outputs: string[];
  description: string;
  needs_volume: boolean;
}

export interface TemplateMeta {
  key: string;
  name: string;
  description: string;
  category: "directional" | "options";
  timeframe: string;
  legs: number;
  indicators: string[];
}

export interface InstrumentHit {
  instrument_key: string;
  trading_symbol: string;
  name: string;
  exchange: string;
  segment: string;
  instrument_type: string;
  lot_size: number;
  expiry: string | null;
  strike_price: number | null;
}

export interface Dataset {
  instrument_key: string;
  timeframe: string;
  bars: number;
  first_bar: string;
  last_bar: string;
  source: string;
  synthetic: boolean;
}

export interface Metrics {
  starting_capital: number;
  ending_equity: number;
  total_return_pct: number;
  cagr_pct: number;
  sharpe: number;
  sortino: number;
  calmar: number;
  volatility_pct: number;
  max_drawdown_pct: number;
  max_drawdown_value: number;
  max_drawdown_days: number;
  total_trades: number;
  winners: number;
  losers: number;
  win_rate_pct: number;
  profit_factor: number;
  expectancy: number;
  avg_win: number;
  avg_loss: number;
  largest_win: number;
  largest_loss: number;
  avg_bars_held: number;
  max_consecutive_wins: number;
  max_consecutive_losses: number;
  gross_pnl: number;
  total_fees: number;
  net_pnl: number;
  cost_drag_pct: number;
  bars: number;
  trading_days: number;
  exposure_pct: number;
  period_start: string | null;
  period_end: string | null;
  account_blown: boolean;
  blown_at: string | null;
}

export interface Trade {
  strategy_id: string;
  leg_id: string;
  symbol: string;
  direction: "LONG" | "SHORT";
  quantity: number;
  entry_time: string;
  entry_price: number;
  exit_time: string;
  exit_price: number;
  gross_pnl: number;
  fees: number;
  net_pnl: number;
  return_pct: number;
  reason: string;
  bars_held: number;
}

export interface Candle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface SeriesLine {
  id: string;
  type: string;
  output: string;
  data: { time: number; value: number }[];
}

export interface Marker {
  time: number;
  position: string;
  color: string;
  shape: string;
  text: string;
}

export interface BacktestResult {
  strategy_id: string;
  strategy_hash: string;
  symbol: string;
  timeframe: string;
  segment: string;
  synthetic_data: boolean;
  data?: DataInfo;
  run_id?: string;
  trials?: number;
  family?: string;
  family_trials?: { runs: number; variants: number };
  deflated?: Deflated;
  holdout?: { start: string; final: boolean; note: string | null } | null;
  warnings: string[];
  metrics: Metrics;
  split: {
    split_date: string;
    in_sample: Metrics;
    out_of_sample: Metrics;
    degradation_pct: number;
    verdict: string;
  } | null;
  trades: Trade[];
  equity_curve: { time: number; equity: number; drawdown: number }[];
  costs: {
    segment: string;
    reference_price: number;
    quantity: number;
    round_trip: Record<string, number>;
    breakeven_move: number;
    breakeven_pct: number;
  };
  blocked_reasons: Record<string, number>;
  chart?: {
    candles: Candle[];
    overlays: SeriesLine[];
    panes: SeriesLine[];
    markers: Marker[];
    truncated: boolean;
    total_bars: number;
  };
}

export interface ValidationResult {
  valid: boolean;
  errors?: { field: string; message: string }[];
  id?: string;
  hash?: string;
  summary?: string;
  is_intraday?: boolean;
  has_options?: boolean;
  document?: Record<string, unknown>;
}

export interface Status {
  auth: { valid: boolean; reason: string; expires_at: string | null; hours_remaining: number };
  instruments: number;
  datasets: number;
  mode: string;
  risk_configured: boolean;
}

// --- live / paper sessions -------------------------------------------------

export interface LivePosition {
  leg_id: string;
  symbol: string;
  quantity: number;
  direction: "LONG" | "SHORT";
  average_price: number;
  last_price: number;
  unrealized_pnl: number;
  stop_price: number | null;
  target_price: number | null;
  trail_armed: boolean;
  opened_at: string | null;
}

export interface LiveBar {
  t: string;
  o: number; h: number; l: number; c: number; v: number;
  entry: boolean;
  exit: boolean;
  blocked: string | null;
}

export interface FeedStatus {
  connected: boolean;
  bars_emitted: number;
  last_bar_at: string | null;
  last_poll_at: string | null;
  reconnects: number;
  errors: string[];
}

export interface LiveSnapshot {
  session_id: string;
  state: string;
  running?: boolean;
  from_journal?: boolean;
  mode: string;
  resumed: boolean;
  strategy: { name: string; hash: string; timeframe: string };
  symbol: string;
  started_at: string | null;
  stopped_at: string | null;
  stop_reason: string;
  bars_seen: number;
  last_bar: { t: string; o: number; h: number; l: number; c: number; v: number } | null;
  starting_capital: number;
  equity: number;
  realized_pnl: number;
  unrealized_pnl: number;
  fees_paid: number;
  net_pnl: number;
  open_positions: LivePosition[];
  trades: Trade[];
  feed: FeedStatus;
  risk: { halted: boolean; reason: string };
  blocked: Record<string, number>;
  reconciled: boolean;
  warnings: string[];
  errors: string[];
  pending_orders: number;
  bars?: LiveBar[];
}

export interface SessionRow {
  session_id: string;
  running: boolean;
  state: string;
  strategy: string;
  symbol: string;
  bars_seen: number;
  trades: number;
  net_pnl?: number;
  started_at?: string | null;
  last_bar_at?: string | null;
  open_legs?: string[];
  errors?: string[];
}

export interface LiveStartRequest {
  strategy: unknown;
  symbol?: string;
  starting_capital: number;
  lot_size: number;
  slippage_bps?: number;
  max_leverage?: number;
  warmup_bars?: number;
  alerts?: boolean;
  source: "live" | "replay";
  replay_start?: string;
  replay_end?: string;
  replay_speed?: number;
  poll_seconds?: number;
}


// ---------------------------------------------------------------------------
// Strategy builder conversations, saved strategies, runs, LLM settings
// ---------------------------------------------------------------------------

export interface Question {
  question: string;
  why: string;
  options: string[];
}

export interface ReadbackCard {
  title: string;
  summary: string;
  sections: { heading: string; lines: string[] }[];
}

export type Pending =
  | { type: "questions"; questions: Question[] }
  | { type: "approval"; card: ReadbackCard; assumptions: string[]; document: Record<string, unknown> }
  | { type: "followup" };

export interface RunSummary {
  run_id: string | null;
  strategy_hash: string;
  symbol: string;
  timeframe: string;
  synthetic_data: boolean;
  metrics: Metrics;
  warnings: string[];
  blocked_reasons: Record<string, number>;
  data: DataInfo;
  trades: number;
  trials: number;
  family?: string;
  family_trials?: { runs: number; variants: number };
  deflated?: Deflated;
}

/** A Sharpe ratio deflated by the number of distinct tests of the idea (backend/analytics/validation.py). */
export interface Deflated {
  sharpe: number | null;
  days: number;
  trials: number;
  psr: number | null;
  benchmark_sharpe: number | null;
  dsr: number | null;
  threshold: number;
  verdict: string;
}

export interface DataInfo {
  instrument_key: string;
  timeframe: string;
  source: string;
  derived_from: string | null;
  synthetic: boolean;
  first_bar: string | null;
  last_bar: string | null;
  bars: number;
  fingerprint: string;
  missing_sessions: number;
  fetched_sessions: number;
  notes: string[];
}

export interface TranscriptEvent {
  role: "user" | "assistant" | "system";
  kind: string;
  text: string;
  data?: Record<string, any>;
}

export type Phase =
  | "working" | "awaiting_answers" | "awaiting_approval" | "awaiting_followup"
  | "failed" | "unsupported" | "rejected" | "interrupted" | "idle" | "compiling";

export interface Conversation {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  strategy_id: string | null;
  version: number | null;
  phase: Phase;
  pending: Pending | null;
  transcript: TranscriptEvent[];
  document: Record<string, unknown> | null;
  assumptions: string[];
  run: RunSummary | null;
  sweep?: SweepResult | null;
  error: string | null;
  usage: {
    steps?: number; total?: number; seconds?: number;
    limits: { steps: number; tokens: number; seconds: number };
  };
  job: { id: string; status: string; progress: number; message: string; error: string | null } | null;
  can_retry: boolean;
}

export interface ConversationRow {
  id: string;
  title: string;
  strategy_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface SavedStrategy {
  id: string;
  name: string;
  hash: string;
  version: number;
  archived: boolean;
  runs: number;
  created_at: string;
  updated_at: string;
}

export interface StrategyVersion {
  strategy_id: string;
  version: number;
  hash: string;
  name: string;
  origin: string;
  note: string;
  created_at: string;
  document?: Record<string, unknown>;
}

export interface RunRow {
  id: string;
  strategy_id: string | null;
  strategy_hash: string;
  strategy_name: string;
  params: Record<string, unknown>;
  data: DataInfo;
  metrics: Metrics;
  created_at: string;
}

export interface LLMStatus {
  provider: string;
  model: string;
  effort: string;
  key_source: "keychain" | "environment" | null;
  configured: boolean;
  limits: { tokens_per_compile: number; model_calls_per_compile: number; seconds_per_compile: number };
}

export interface SweepAxisSummary {
  path: string;
  label: string;
  values: number[];
}

export interface SweepRow {
  label: string;
  assignment: Record<string, number>;
  run_id: string;
  strategy_hash: string;
  metrics: {
    total_trades: number | null;
    net_pnl: number | null;
    total_return_pct: number | null;
    sharpe: number | null;
    max_drawdown_pct: number | null;
    win_rate_pct: number | null;
    profit_factor: number | null;
  };
}

export interface SweepResult {
  family: string;
  family_trials: { runs: number; variants: number } | null;
  axes: SweepAxisSummary[];
  symbol: string | null;
  timeframe: string;
  synthetic_data: boolean | null;
  data: { source?: string; first_bar?: string; last_bar?: string; bars?: number } | null;
  variants: SweepRow[];
  skipped: { label: string; assignment: Record<string, number>; reason: string }[];
  note: string;
}

export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export interface Job<R = unknown> {
  id: string;
  kind: string;
  status: JobStatus;
  progress: number;
  message: string;
  result: R | null;
  error: string | null;
}

export const TERMINAL_JOB: JobStatus[] = ["succeeded", "failed", "cancelled"];

export type ScanStatus =
  | "triggered_today" | "holding" | "lapsed" | "waiting" | "not_enough_data" | "no_data" | "error" | "ok";

export interface ScanRow {
  symbol: string;
  key: string;
  status: ScanStatus;
  reason?: string;
  as_of?: string;
  close?: number;
  active?: boolean;
  distance_pct?: number | null;
  since?: string | null;
  since_close?: number | null;
  since_capped?: boolean;
  last_fired?: string | null;
  last_fired_close?: number | null;
  bars_since?: number | null;
  stale?: boolean;
  data?: { source: string; synthetic: boolean; first_bar: string; last_bar: string; bars: number };
  events?: ScanEvent[];
}

export interface ScanEvent {
  symbol?: string;
  date: string;
  close: number;
  forward: Record<string, number | null>;
}

export interface ReturnStats { count: number; mean: number | null; median: number | null; hit_rate: number | null }

export interface ScanResult {
  mode: "now" | "history";
  spec_hash: string;
  name: string;
  side: "long" | "short" | null;
  universe: number;
  counts: Partial<Record<ScanStatus, number>>;
  notes: string[];
  as_of?: string | null;
  rows: ScanRow[];
  events?: ScanEvent[];
  summary?: Record<string, { signal: ReturnStats; baseline: ReturnStats }>;
}

export interface ScanValidation {
  valid: boolean;
  hash?: string;
  universe?: number | null;
  errors: { field: string; message: string }[];
}

export interface SavedScan {
  id: string;
  name: string;
  version: number;
  hash: string;
  alerts: boolean;
  archived: boolean;
  last_checked_at: string | null;
  created_at: string;
  updated_at: string;
  document?: Record<string, unknown>;
}

export interface ScanEventRow {
  id: number;
  as_of: string | null;
  symbol: string;
  key: string;
  kind: "triggered" | "back_inside";
  detail: { status?: string; since?: string | null; close?: number | null; distance_pct?: number | null };
  created_at: string;
}

export interface SavedScanDetail extends SavedScan {
  document: Record<string, unknown>;
  events: ScanEventRow[];
  last_snapshot: { id: number; version: number; as_of: string | null; created_at: string } | null;
}

export interface ScanCheckResult {
  scan_id: string;
  as_of: string | null;
  new_session?: boolean;
  baseline?: boolean;
  scanned: number;
  active: number;
  events: { symbol: string; kind: string }[];
  sent: boolean;
  message?: string;
}

export interface PrefetchPlan {
  timeframe: string;
  start: string;
  end: string;
  instruments: number;
  to_download: number;
  up_to_date: number;
  skipped: number;
  missing_sessions: number;
  calls: number;
  eta_seconds: number;
  binding_limit: string | null;
  broker_ready: boolean;
  broker_reason: string | null;
  rows: { key: string; symbol: string; calls?: number; missing_sessions?: number; skip?: string }[];
}

export interface PrefetchResult {
  instruments: number;
  sessions_fetched: number;
  sessions_failed: number;
  errors: { key: string; symbol: string; error: string }[];
  elapsed_seconds: number;
}

export interface ScanCompileResult {
  status: "ready" | "needs_clarification" | "unsupported" | "failed";
  spec?: Record<string, unknown>;
  card?: ReadbackCard;
  assumptions?: string[];
  questions?: Question[];
  dialogue?: { role: string; content: string }[];
  change_summary?: string;
  reason?: string;
  error?: string;
}

export interface BuilderBacktestSettings {
  start?: string;
  end?: string;
  starting_capital?: number;
  lot_size?: number;
}

class ApiError extends Error {
  constructor(message: string, readonly status: number, readonly detail?: unknown) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });

  if (!response.ok) {
    let detail: unknown;
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      detail = body.detail ?? body;
      // FastAPI puts our message in `detail`; surface it rather than a bare code.
      if (typeof body.detail === "string") message = body.detail;
    } catch {
      /* body was not JSON */
    }
    throw new ApiError(message, response.status, detail);
  }

  return response.json() as Promise<T>;
}

export const api = {
  status: () => request<Status>("/api/status"),

  indicators: () =>
    request<{ indicators: IndicatorMeta[]; categories: Record<string, string[]> }>(
      "/api/indicators",
    ),

  templates: () => request<{ templates: TemplateMeta[] }>("/api/templates"),

  template: (key: string, symbol?: string) =>
    request<Record<string, unknown>>(
      `/api/templates/${key}${symbol ? `?symbol=${encodeURIComponent(symbol)}` : ""}`,
    ),

  searchInstruments: (q: string, limit = 12) =>
    request<{ results: InstrumentHit[] }>(
      `/api/instruments/search?q=${encodeURIComponent(q)}&limit=${limit}`,
    ),

  datasets: () => request<{ datasets: Dataset[] }>("/api/datasets"),

  simulatorProfiles: () =>
    request<{ profiles: { name: string; spot: number; annual_vol: number; lot_size: number }[] }>(
      "/api/simulator/profiles",
    ),

  validate: (document: unknown) =>
    request<ValidationResult>("/api/strategies/validate", {
      method: "POST",
      body: JSON.stringify(document),
    }),

  backtest: (payload: {
    strategy: unknown;
    symbol?: string;
    starting_capital: number;
    lot_size: number;
    slippage_bps?: number;
    max_leverage?: number;
    include_chart?: boolean;
  }) =>
    request<BacktestResult>("/api/backtest", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  sweep: (payload: {
    strategy: unknown;
    strategy_id?: string;
    symbol?: string;
    starting_capital: number;
    lot_size: number;
    slippage_bps?: number;
    max_leverage?: number;
  }) =>
    request<Job<SweepResult>>("/api/jobs/sweep", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  validateScan: (spec: unknown) =>
    request<ScanValidation>("/api/scans/validate", { method: "POST", body: JSON.stringify(spec) }),

  scan: (payload: { spec: unknown; mode: "now" | "history"; fetch?: boolean }) =>
    request<Job<ScanResult>>("/api/jobs/scan", { method: "POST", body: JSON.stringify(payload) }),

  savedScans: () =>
    request<{ scans: SavedScan[]; alert_times: string; telegram: boolean }>("/api/scans"),

  savedScan: (id: string) => request<SavedScanDetail>(`/api/scans/${encodeURIComponent(id)}`),

  saveScan: (document: unknown, id?: string, alerts?: boolean) =>
    request<SavedScan & { new_version?: boolean }>(id ? `/api/scans/${encodeURIComponent(id)}` : "/api/scans", {
      method: id ? "PUT" : "POST",
      body: JSON.stringify({ document, ...(alerts === undefined ? {} : { alerts }) }),
    }),

  archiveScan: (id: string) =>
    request<{ id: string; archived: boolean }>(`/api/scans/${encodeURIComponent(id)}`, { method: "DELETE" }),

  checkScan: (id: string) =>
    request<Job<ScanCheckResult>>(`/api/scans/${encodeURIComponent(id)}/check`, { method: "POST" }),

  prefetchPlan: (universe: unknown, timeframe: "1d" | "1m" = "1d") =>
    request<PrefetchPlan>("/api/data/prefetch/plan", { method: "POST", body: JSON.stringify({ universe, timeframe }) }),

  prefetch: (universe: unknown, timeframe: "1d" | "1m" = "1d") =>
    request<Job<PrefetchResult>>("/api/jobs/prefetch", { method: "POST", body: JSON.stringify({ universe, timeframe }) }),

  resumeJob: <R = unknown>(id: string) =>
    request<Job<R>>(`/api/jobs/${encodeURIComponent(id)}/resume`, { method: "POST" }),

  compileScan: (payload: { text: string; dialogue?: { role: string; content: string }[]; current?: unknown }) =>
    request<Job<ScanCompileResult>>("/api/scans/compile", { method: "POST", body: JSON.stringify(payload) }),

  finalTest: (payload: { strategy: unknown; symbol?: string; starting_capital: number; lot_size: number;
    slippage_bps?: number; max_leverage?: number }) =>
    request<Job<{ run_id: string }>>("/api/jobs/final-test", { method: "POST", body: JSON.stringify(payload) }),

  job: <R = unknown>(id: string) => request<Job<R>>(`/api/jobs/${encodeURIComponent(id)}`),

  cancelJob: (id: string) =>
    request<Job>(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST" }),

  fetchData: (payload: { symbol: string; timeframe: string; days: number }) =>
    request<{ instrument_key: string; fetched: number; total_bars: number; coverage_pct: number; clean: boolean }>(
      "/api/data/fetch",
      { method: "POST", body: JSON.stringify(payload) },
    ),

  simulate: (payload: { profile: string; timeframe: string; days: number; seed: number }) =>
    request<{ instrument_key: string; bars: number }>("/api/data/simulate", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  liveStart: (payload: LiveStartRequest) =>
    request<{ session_id: string; started: boolean; already_running: boolean; source: string }>(
      "/api/live/start",
      { method: "POST", body: JSON.stringify(payload) },
    ),

  liveStop: (sessionId: string) =>
    request<{ session_id: string; snapshot: LiveSnapshot }>(
      `/api/live/${encodeURIComponent(sessionId)}/stop`,
      { method: "POST" },
    ),

  liveSessions: () =>
    request<{ active: SessionRow[]; history: SessionRow[] }>("/api/live/sessions"),

  liveSnapshot: (sessionId: string, includeBars = true) =>
    request<LiveSnapshot>(
      `/api/live/${encodeURIComponent(sessionId)}?include_bars=${includeBars}`,
    ),

  // -- builder ---------------------------------------------------------------

  conversations: () =>
    request<{ conversations: ConversationRow[] }>("/api/agent/conversations"),

  startConversation: (text: string, backtest: BuilderBacktestSettings = {}) =>
    request<Conversation>("/api/agent/conversations", {
      method: "POST",
      body: JSON.stringify({ text, backtest }),
    }),

  conversation: (id: string) =>
    request<Conversation>(`/api/agent/conversations/${encodeURIComponent(id)}`),

  reply: (id: string, payload: Record<string, unknown>) =>
    request<Conversation>(`/api/agent/conversations/${encodeURIComponent(id)}/reply`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  retryTurn: (id: string) =>
    request<Conversation>(`/api/agent/conversations/${encodeURIComponent(id)}/retry`, {
      method: "POST",
    }),

  cancelTurn: (id: string) =>
    request<Conversation>(`/api/agent/conversations/${encodeURIComponent(id)}/cancel`, {
      method: "POST",
    }),

  // -- saved strategies and runs ---------------------------------------------

  strategies: () => request<{ strategies: SavedStrategy[] }>("/api/strategies"),

  strategy: (id: string, version?: number) =>
    request<StrategyVersion & { document: Record<string, unknown>; versions: StrategyVersion[] }>(
      `/api/strategies/${encodeURIComponent(id)}${version ? `?version=${version}` : ""}`,
    ),

  saveStrategy: (document: unknown, id?: string, note = "") =>
    request<StrategyVersion & { new_version: boolean }>(
      id ? `/api/strategies/${encodeURIComponent(id)}` : "/api/strategies",
      { method: id ? "PUT" : "POST", body: JSON.stringify({ document, note }) },
    ),

  archiveStrategy: (id: string) =>
    request<{ id: string; archived: boolean }>(`/api/strategies/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),

  runs: (strategyId?: string, limit = 50) =>
    request<{ runs: RunRow[] }>(
      `/api/runs?limit=${limit}${strategyId ? `&strategy_id=${encodeURIComponent(strategyId)}` : ""}`,
    ),

  run: (id: string) =>
    request<RunRow & { document: Record<string, unknown>; result: BacktestResult }>(
      `/api/runs/${encodeURIComponent(id)}`,
    ),

  // -- LLM settings ------------------------------------------------------------

  llmStatus: () => request<LLMStatus>("/api/llm/status"),

  setLlmKey: (apiKey: string) =>
    request<LLMStatus>("/api/llm/key", {
      method: "PUT",
      body: JSON.stringify({ provider: "anthropic", api_key: apiKey }),
    }),

  deleteLlmKey: () => request<LLMStatus>("/api/llm/key", { method: "DELETE" }),
};

export { ApiError };
