// VFP: Talking to the terminal's trading endpoints and turning refusal reasons into words.
// Changes when: a trading action or a pre-trade reason is added.
// Anti-goal:
// 1. Retrying an order action by itself — every click is one request; the terminal decides what happens next.

import { apiSend } from "./session";

const REASONS: Record<string, string> = {
  trading_disabled: "торговля выключена в config.toml ([trading] enabled)",
  pair_busy: "по этой монете уже идёт операция",
  keys_not_accepted: "ключи не приняты — проверьте в Настройках",
  pair_not_tradable: "пара подозрительная или в чёрном списке",
  stale: "данные устарели",
  no_quote: "нет котировки по стакану",
  roi_below_entry: "ROI ниже порога входа",
  max_open_pairs: "достигнут лимит открытых пар",
  max_total_usd: "превышен общий объём",
  token_already_open: "по этой монете уже открыта пара",
  not_warmed_up: "плечо и маржа ещё не выставлены",
  positions_unknown: "позиции не получены с бирж — повторите",
  leg_close_failed: "нога не закрылась — пара помечена «нога потеряна»",
  close_incomplete: "закрытие не завершено",
  unknown_trade: "пара не найдена",
  pair_not_in_catalog: "пары нет в каталоге",
  no_book: "нет стакана",
  book_too_thin: "глубины не хватает на размер",
  size_below_common_step: "размер меньше шага количества",
  no_quantity: "количество не рассчитано",
};

export function reasonText(reason: string): string {
  const [code, detail] = reason.split(":");
  const scoped: Record<string, string> = {
    insufficient_margin: "не хватает свободной маржи",
    balance_unknown: "баланс не получен",
    exchange_blocked: "биржа заблокирована",
    exchange_read_only: "у биржи нет торгового API, только наблюдение",
    below_min_qty: "меньше минимального количества",
    below_min_notional: "меньше минимальной суммы",
    above_max_market_qty: "больше максимума рыночного ордера",
  };
  if (scoped[code]) return `${scoped[code]}${detail ? ` · ${detail.toUpperCase()}` : ""}`;
  return REASONS[reason] ?? reason;
}

export async function tradingAction<T>(path: string, token: string, body?: unknown): Promise<T> {
  try {
    return await apiSend<T>("POST", path, token, body);
  } catch (error) {
    const text = (error as Error).message;
    throw new Error(text);
  }
}

export function explainFailure(error: unknown): string {
  const text = (error as Error).message;
  try {
    const parsed = JSON.parse(text) as { reasons?: string[] };
    if (parsed.reasons) return parsed.reasons.map(reasonText).join("; ");
  } catch {
    // plain text
  }
  return text;
}
