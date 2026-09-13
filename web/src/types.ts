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

export interface ExchangeState {
  name: string;
  status: "not_configured" | "configured" | "connected" | "disconnected";
  ping_ms?: number | null;
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
