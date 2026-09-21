// VFP: Shapes of messages the terminal sends to the interface (protocol version 1, see app/api/hub.py).
// Changes when: the live protocol or the snapshot contents change on the server.
// Anti-goal:
// 1. Client-side derived trading numbers — the terminal computes them, the interface only shows them.

export type Level = "info" | "warning" | "critical";

export interface JournalEvent {
  ts_ms: number;
  level: Level;
  source: string;
  type: string;
  exchange: string | null;
  trade_id: number | null;
  payload: Record<string, unknown>;
}

export type KeysState = "none" | "saved" | "checking" | "ok" | "warning" | "rejected";

export interface ExchangeState {
  name: string;
  demo: boolean;
  link: "unknown" | "up" | "down";
  ping_ms: number | null;
  clock_offset_ms: number | null;
  clock_warning: boolean;
  keys: KeysState;
  read_only?: boolean;
}

export interface AccountFacts {
  permissions: {
    reading: boolean | null;
    futures: boolean | null;
    withdrawals: boolean | null;
    ip_restricted: boolean | null;
  };
  one_way_position_mode: boolean | null;
  wallet_usdt: string | null;
  available_usdt: string | null;
  taker_fee_pct: string | null;
  maker_fee_pct: string | null;
  ping_ms: number | null;
  clock_offset_ms: number | null;
  errors: string[];
  notes: string[];
}

export interface CheckResult {
  checked_at_ms: number;
  accepted: boolean;
  blocking: string[];
  warnings: string[];
  facts: AccountFacts;
}

export interface ExchangeDetails {
  name: string;
  demo: boolean;
  link: ExchangeState["link"];
  ping_ms: number | null;
  clock_offset_ms: number | null;
  probe_error: string | null;
  key_masked: string | null;
  keys: KeysState;
  check: CheckResult | null;
  read_only?: boolean;
  needs_passphrase?: boolean;
}

export interface InstrumentsSummary {
  pairs: number;
  tradable: number;
  suspicious: number;
  blacklisted: number;
  refreshed_at_ms: number | null;
  refreshing: boolean;
  error: string | null;
}

export interface PairLeg {
  exchange: string;
  symbol: string;
  url: string | null;
  qty_unit_tokens: string;
  price_unit_tokens: string;
  step_tokens: string;
  min_qty_tokens: string;
  min_notional_usd: string;
  price_per_token: string | null;
}

export interface PairView {
  key: string;
  pair_id: number | null;
  token: string;
  a: PairLeg;
  b: PairLeg;
  common_step_tokens: string;
  min_qty_tokens: string;
  price_gap_pct: string | null;
  index_gap_pct: string | null;
  volume24h_weak_usd: string | null;
  reason: string | null;
  manually_verified: boolean;
  blacklisted: boolean;
  suspicious: boolean;
  tradable: boolean;
}

export interface FeedLeg {
  exchange: string;
  symbol: string;
  url: string | null;
}

export interface FeedRow {
  key: string;
  token: string;
  long: FeedLeg | null;
  short: FeedLeg | null;
  qty_tokens: string | null;
  roi_net_pct: string | null;
  capacity_usd: string | null;
  phase: "candidate" | "in_feed" | "tracking" | null;
  lifetime_ms: number | null;
  volume24h_weak_usd: string | null;
  suspicious: boolean;
  blacklisted: boolean;
  block: string | null;
  open_blocks?: string[];
  profit_usd: string | null;
  funding: {
    long: { rate_pct: string; interval_h: string; next_ms: number | null } | null;
    short: { rate_pct: string; interval_h: string; next_ms: number | null } | null;
    hourly_pct: string | null;
    horizon_pct: string | null;
    horizon_usd: string | null;
    next_ms: number | null;
    next_pct: string | null;
    next_usd: string | null;
  } | null;
  funding_known: boolean;
  total_pct: string | null;
  total_usd: string | null;
  score: number | null;
  score_parts: { result: number; depth: number; stability: number; liquidity: number } | null;
}

export interface TradingStatus {
  enabled: boolean;
  busy: string[];
  warmed: number;
  warm_errors: { exchange: string; symbol: string; error: string }[];
  blocked: Record<string, string>;
  private_streams?: Record<string, boolean>;
  settings: Record<string, unknown>;
}

export interface FeedView {
  rows: FeedRow[];
  radar: FeedRow[];
  settings?: {
    size_usd: string;
    min_roi_pct: string;
    enter_after_ms: number;
    funding_horizon_h: string;
    fresh_ms: number;
    taker_fee_pct: Record<string, string>;
  };
  stats: Record<string, number>;
  streams?: Record<string, Partial<Record<"connections" | "sockets" | "depth_symbols" | "polls" | "poll_errors" | "listings" | "reconnects", number>>>;
  funding?: Record<string, { contracts: number; age_s: number | null; errors: number }>;
}

export interface TradeCard {
  id: string;
  token: string;
  status: "opening" | "open" | "closing" | "leg_lost" | "closed";
  issues: string[];
  long: { exchange: string; symbol: string; entry: string | null };
  short: { exchange: string; symbol: string; entry: string | null };
  qty_tokens: string | null;
  opened_at_ms: number;
  fees_usd: string | null;
  funding_usd: string | null;
  entry_spread_pct?: string | null;
  exit_spread_pct?: string | null;
  pnl_now_usd?: string | null;
  pnl_now_pct?: string | null;
  long_now?: string | null;
  short_now?: string | null;
  funding_long?: { rate_pct: string; interval_h: string } | null;
  funding_short?: { rate_pct: string; interval_h: string } | null;
  funding_hourly_usd?: string | null;
  funding_next_ms?: number | null;
  funding_next_usd?: string | null;
  liq_worst_pct?: string | null;
  liq_long_pct?: string | null;
  liq_short_pct?: string | null;
  book_age_ms?: number | null;
}

export interface PositionView {
  exchange: string;
  symbol: string;
  token: string;
  side: "long" | "short";
  qty_tokens: string | null;
  entry: string | null;
  liquidation: string | null;
  leverage: number | null;
}

export interface PortfolioView {
  trades: TradeCard[];
  foreign: PositionView[];
  suggestions: { token: string; long: PositionView; short: PositionView }[];
  accounts: Record<string, { polled_ms: number | null; error: string | null; positions: number; equity_usd: string | null; available_usd: string | null }>;
}

export interface Snapshot {
  app: { version: string; started_ts_ms: number };
  portfolio?: PortfolioView;
  trading?: TradingStatus;
  instruments?: InstrumentsSummary;
  feed?: FeedView;
  history?: { recorded: number; open: number; radar_snapshots: number };
  database: {
    status: "ok" | "down";
    error: string | null;
    pending_rows: number;
    spooled_rows: number;
    rejected_rows: number;
  };
  exchanges: ExchangeState[];
  pairs: { open: number; limit: number; sleep_blocked?: boolean };
}

export type ServerMessage =
  | { type: "hello"; protocol: number; server_ts_ms: number; state: Snapshot }
  | { type: "snapshot"; server_ts_ms: number; state: Snapshot }
  | { type: "event"; event: JournalEvent }
  | { type: "ping"; server_ts_ms: number };
