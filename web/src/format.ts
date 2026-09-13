// VFP: Human-readable Russian text for journal events, times and counters.
// Changes when: a new journal event type appears or wording changes.
// Anti-goal:
// 1. Guessing numbers — anything the event does not carry is not shown.

import type { JournalEvent } from "./types";

export function clock(tsMs: number): string {
  return new Date(tsMs).toLocaleTimeString("ru-RU", { hour12: false });
}

export function uptime(fromMs: number, nowMs: number): string {
  const total = Math.max(0, Math.floor((nowMs - fromMs) / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const pad = (value: number) => String(value).padStart(2, "0");
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(seconds)}` : `${minutes}:${pad(seconds)}`;
}

function exchangeName(event: JournalEvent): string {
  return (event.exchange ?? "?").toUpperCase();
}

export function describe(event: JournalEvent): string {
  const payload = event.payload ?? {};
  switch (`${event.source}.${event.type}`) {
    case "app.started":
      return `терминал запущен · версия ${String(payload.version ?? "?")}`;
    case "app.stopped":
      return "терминал остановлен";
    case "storage.db_connected": {
      const applied = Array.isArray(payload.migrations_applied) ? payload.migrations_applied : [];
      const spooled = Number(payload.spooled_rows ?? 0);
      const parts = ["база подключена"];
      if (applied.length) parts.push(`миграции: ${applied.join(", ")}`);
      if (spooled) parts.push(`дописываю из буфера ${spooled} строк`);
      return parts.join(" · ");
    }
    case "storage.db_unavailable":
      return `база недоступна, запись идёт в буфер на диске · ${String(payload.error ?? "")}`;
    case "exchange.link_up":
      return `${exchangeName(event)} на связи · ${String(payload.ping_ms ?? "?")}мс`;
    case "exchange.link_down":
      return `${exchangeName(event)} нет связи · ${String(payload.error ?? "")}`;
    case "exchange.keys_saved":
      return `${exchangeName(event)} ключи сохранены · ${String(payload.key ?? "")}`;
    case "exchange.keys_deleted":
      return `${exchangeName(event)} ключи удалены`;
    case "exchange.check_ok": {
      const warnings = Array.isArray(payload.warnings) ? payload.warnings : [];
      return `${exchangeName(event)} ключ принят${warnings.length ? ` · замечания: ${warnings.join(", ")}` : ""}`;
    }
    case "instruments.refreshed":
      return `пары обновлены · Binance ${String(payload.binance)} · MEXC ${String(payload.mexc)} · общих ${String(payload.pairs)} · подозрительных ${String(payload.suspicious)}`;
    case "instruments.refresh_failed":
      return `не удалось обновить пары · ${String(payload.error ?? "")}`;
    case "instruments.pair_flags":
      return `${String(payload.token)} · ${payload.blacklisted ? "в чёрном списке" : payload.manually_verified ? "проверено вручную" : "отметки сняты"}`;
    case "exchange.check_rejected": {
      const blocking = Array.isArray(payload.blocking) ? payload.blocking : [];
      return `${exchangeName(event)} ключ не принят · ${blocking.join(", ")}`;
    }
    default:
      return `${event.source}.${event.type}${event.exchange ? ` · ${event.exchange.toUpperCase()}` : ""}`;
  }
}
