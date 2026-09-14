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
    case "trading.opening":
      return `${String(payload.token)} · открываю пару · ${String(payload.qty_tokens)} токенов · ROI ожид. ${Number(payload.roi_expected).toFixed(2)}%`;
    case "trading.opened":
      return `${String(payload.token)} · пара открыта · ROI ожид. ${Number(payload.roi_expected).toFixed(2)}% факт ${Number(payload.roi_actual).toFixed(2)}% · ${String(payload.ms_long)}/${String(payload.ms_short)}мс`;
    case "trading.closing":
      return `${String(payload.token)} · закрываю пару`;
    case "trading.closed":
      return `${String(payload.token)} · пара закрыта · PnL $${Number(payload.pnl_net_usd).toFixed(2)}`;
    case "trading.open_rejected":
      return `${String(payload.token)} · обе ноги отклонены, позиций нет`;
    case "trading.leg_rejected_hedge_closed":
      return `${String(payload.token)} · СБОЙ НОГИ · одна нога отклонена, исполненная закрыта`;
    case "trading.partial_fill_equalized":
      return `${String(payload.token)} · частичное исполнение, ноги выровнены`;
    case "trading.partial_fill_closed":
      return `${String(payload.token)} · частичное исполнение ниже минимума, обе ноги закрыты`;
    case "trading.hedge_fix_failed":
      return `${String(payload.token)} · НЕ УДАЛОСЬ ЗАКРЫТЬ ИСПОЛНЕННУЮ НОГУ · ${String(payload.side)}`;
    case "trading.leg_status_unknown":
      return `${String(payload.token)} · СТАТУС ОРДЕРА НЕИЗВЕСТЕН, выясняю`;
    case "trading.leg_close_failed":
      return `${String(payload.token)} · НОГА НЕ ЗАКРЫЛАСЬ`;
    case "trading.warmup_failed":
      return `${exchangeName(event)} · не удалось выставить плечо и маржу · ${String(payload.symbol)} · ${String(payload.error ?? "")}`;
    case "app.stopped_with_open_pairs":
      return `ТЕРМИНАЛ ОСТАНОВЛЕН ПРИ ОТКРЫТЫХ ПАРАХ (${String(payload.pairs)}) — позиции на биржах остаются`;
    case "portfolio.leg_lost":
      return `${String(payload.token)} · НОГА ПОТЕРЯНА · ${(Array.isArray(payload.issues) ? payload.issues : []).join(", ")}`;
    case "portfolio.leg_found":
      return `${String(payload.token)} · нога снова видна на бирже`;
    case "portfolio.pair_assigned":
      return `${String(payload.token)} · пара взята под наблюдение · ${String(payload.qty_tokens)} токенов`;
    case "portfolio.record_closed":
      return `${String(payload.token)} · запись о паре закрыта`;
    case "portfolio.restored":
      return `восстановлено открытых пар: ${String(payload.pairs)}`;
    case "portfolio.liquidation_near":
      return `${String(payload.token)} · до ликвидации ${String(payload.distance_pct)}%`;
    case "history.dangling_episodes_closed":
      return `закрыто незавершённых вилок прошлого запуска: ${String(payload.count)}`;
    case "settings.changed":
      return `настройка ${String(payload.key)}: ${String(payload.old)} → ${String(payload.new)}`;
    case "instruments.refreshed":
    {
      const contracts = (payload.contracts ?? {}) as Record<string, number>;
      const parts = Object.entries(contracts).map(([name, count]) => `${name.toUpperCase()} ${count}`);
      return `пары обновлены · ${parts.join(" · ")} · пар ${String(payload.pairs)} · подозрительных ${String(payload.suspicious)}`;
    }
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
