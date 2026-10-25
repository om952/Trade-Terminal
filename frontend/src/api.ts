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

export interface InterruptedSession {
  session_id: string;
  strategy_name: string;
  symbol: string;
  timeframe: string;
  cause: "crash" | "app shutdown";
  bars_seen: number;
  trades: number;
  open_legs: string[];
  last_bar_at: string | null;
  resume: LiveStartRequest;
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

  liveInterrupted: () =>
    request<{ sessions: InterruptedSession[] }>("/api/live/interrupted"),

  liveSnapshot: (sessionId: string, includeBars = true) =>
    request<LiveSnapshot>(
      `/api/live/${encodeURIComponent(sessionId)}?include_bars=${includeBars}`,
    ),
};

export { ApiError };
