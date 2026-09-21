// VFP: One live connection to the terminal — authenticates, answers heartbeats, reconnects, and says when the data went stale.
// Changes when: the live protocol, reconnect policy or staleness rule changes.
// Anti-goal:
// 1. Showing old numbers as if they were live — no message for STALE_AFTER_MS marks the whole screen stale.
// 2. Hammering a stopped terminal — reconnects back off up to MAX_BACKOFF_MS.

import { useEffect, useRef, useState } from "react";
import type { JournalEvent, ServerMessage, Snapshot } from "./types";

const STALE_AFTER_MS = 3000;
const MAX_BACKOFF_MS = 5000;
const JOURNAL_LIMIT = 500;

export type LinkState = "connecting" | "live" | "stale" | "unauthorized";

export interface Live {
  link: LinkState;
  snapshot: Snapshot | null;
  events: JournalEvent[];
  /** When the last message arrived. Shown as a clock: it ticks while data flows and stops the moment it does not. */
  lastMessageMs: number;
}

export function useLive(token: string, initialEvents: JournalEvent[]): Live {
  const [link, setLink] = useState<LinkState>("connecting");
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [events, setEvents] = useState<JournalEvent[]>([]);
  const lastMessageAt = useRef(0);

  useEffect(() => {
    setEvents((current) => mergeEvents(initialEvents, current));
  }, [initialEvents]);

  useEffect(() => {
    let socket: WebSocket | null = null;
    let retryTimer: number | undefined;
    let attempt = 0;
    let stopped = false;

    const connect = () => {
      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(`${scheme}://${window.location.host}/ws`);
      socket.onopen = () => socket?.send(JSON.stringify({ type: "auth", token }));
      socket.onmessage = (message) => {
        lastMessageAt.current = Date.now();
        const data = JSON.parse(message.data as string) as ServerMessage;
        switch (data.type) {
          case "hello":
            attempt = 0;
            setSnapshot(data.state);
            setLink("live");
            break;
          case "snapshot":
            setSnapshot(data.state);
            setLink("live");
            break;
          case "event":
            setEvents((current) => mergeEvents(current, [data.event]));
            break;
          case "ping":
            socket?.send(JSON.stringify({ type: "pong", server_ts_ms: data.server_ts_ms }));
            break;
        }
      };
      socket.onclose = (event) => {
        if (stopped) return;
        if (event.code === 4401) {
          setLink("unauthorized");
          return;
        }
        setLink((current) => (current === "live" ? "stale" : current));
        const delay = Math.min(MAX_BACKOFF_MS, 250 * 2 ** attempt);
        attempt += 1;
        retryTimer = window.setTimeout(connect, delay);
      };
    };

    connect();
    const staleTimer = window.setInterval(() => {
      if (lastMessageAt.current && Date.now() - lastMessageAt.current > STALE_AFTER_MS) {
        setLink((current) => (current === "live" ? "stale" : current));
      }
    }, 500);

    return () => {
      stopped = true;
      window.clearTimeout(retryTimer);
      window.clearInterval(staleTimer);
      socket?.close();
    };
  }, [token]);

  // Read off the ref at render time: the screen re-renders on every snapshot and once a second besides,
  // so the clock in the footer advances without a state update per message.
  return { link, snapshot, events, lastMessageMs: lastMessageAt.current };
}

function mergeEvents(base: JournalEvent[], extra: JournalEvent[]): JournalEvent[] {
  const seen = new Set(base.map(eventKey));
  const merged = base.concat(extra.filter((event) => !seen.has(eventKey(event))));
  merged.sort((a, b) => a.ts_ms - b.ts_ms);
  return merged.slice(-JOURNAL_LIMIT);
}

function eventKey(event: JournalEvent): string {
  return `${event.ts_ms}|${event.source}|${event.type}|${event.exchange ?? ""}|${event.trade_id ?? ""}`;
}
