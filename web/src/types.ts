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
  long_avg: string | null;
  short_avg: string | null;
  qty_tokens: string | null;
  roi_net_pct: string | null;
  roi_gross_pct: string | null;
  roi_top_pct: number | null;
  exit_spread_pct: string | null;
  capacity_usd: string | null;
  age_long_ms: number | null;
  age_short_ms: number | null;
  phase: "candidate" | "in_feed" | "tracking" | null;
  lifetime_ms: number | null;
  roi_peak_pct: string | null;
  volume24h_weak_usd: string | null;
  suspicious: boolean;
  blacklisted: boolean;
  block: string | null;
}

export interface FeedView {
  rows: FeedRow[];
  radar: FeedRow[];
  settings?: { size_usd: string; min_roi_pct: string; fresh_ms: number; taker_fee_pct: Record<string, string> };
  stats: Record<string, number>;
  streams?: Record<string, Record<string, number>>;
}

export interface Snapshot {
  app: { version: string; started_ts_ms: number };
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
  pairs: { open: number; limit: number };
}

export type ServerMessage =
  | { type: "hello"; protocol: number; server_ts_ms: number; state: Snapshot }
  | { type: "snapshot"; server_ts_ms: number; state: Snapshot }
  | { type: "event"; event: JournalEvent }
  | { type: "ping"; server_ts_ms: number };
