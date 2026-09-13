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

export interface Snapshot {
  app: { version: string; started_ts_ms: number };
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
