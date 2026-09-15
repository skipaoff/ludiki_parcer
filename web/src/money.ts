// VFP: How money, percentages, prices and funding read on screen — signs, units, colour class and time to the next payment.
// Changes when: the wording or precision of trading numbers changes.
// Anti-goal:
// 1. Computing trading numbers — values arrive from the terminal; this file only formats them.

function signed(number: number, text: string): string {
  // A value that rounds to zero carries no sign: "+$0.00" would read as a gain.
  if (Number(text) === 0) return text;
  return `${number > 0 ? "+" : "−"}${text}`;
}

export function signedUsd(value: string | number | null | undefined, digits = 2): string {
  if (value == null) return "—";
  const number = Number(value);
  const text = Math.abs(number).toFixed(digits);
  return Number(text) === 0 ? `$${text}` : `${number > 0 ? "+" : "−"}$${text}`;
}

export function signedPct(value: string | number | null | undefined, digits = 2): string {
  if (value == null) return "—";
  const number = Number(value);
  return `${signed(number, Math.abs(number).toFixed(digits))}%`;
}

/** "gain" for money made, "loss" for money lost, nothing for zero or unknown. */
export function signClass(value: string | number | null | undefined): string {
  if (value == null) return "";
  const number = Number(value);
  return number > 0 ? "gain" : number < 0 ? "loss" : "";
}

export function price(value: string | null | undefined): string {
  if (value == null) return "—";
  const number = Number(value);
  if (number >= 1) return number.toLocaleString("en-US", { maximumFractionDigits: 4 });
  return number.toFixed(Math.min(15, 4 - Math.floor(Math.log10(number))));
}

export interface RateView {
  rate_pct: string;
  interval_h: string;
}

/** A leg's funding as the exchange states it: percent per settlement and the settlement interval, "+0.005%/4ч", "−0.0069%/1ч". */
export function rateText(rate: RateView | null | undefined): string {
  if (!rate) return "фанд. ?";
  const hours = Number(rate.interval_h);
  const interval = Number.isInteger(hours) ? `${hours}ч` : `${Math.round(hours * 60)}м`;
  const number = Number(rate.rate_pct);
  const text = Math.abs(number).toFixed(4).replace(/0$/, "");
  return `${signed(number, text)}%/${interval}`;
}

/** "23м", "1ч 05м", "сейчас". */
export function until(targetMs: number | null | undefined, nowMs: number): string {
  if (targetMs == null) return "—";
  const minutes = Math.max(0, Math.round((targetMs - nowMs) / 60000));
  if (minutes === 0) return "сейчас";
  if (minutes < 60) return `${minutes}м`;
  return `${Math.floor(minutes / 60)}ч ${String(minutes % 60).padStart(2, "0")}м`;
}

/** Coarse holding time that does not flicker every second: "<1 мин", "3 мин", "1 ч 05 мин". */
export function coarseDuration(ms: number | null | undefined): string {
  if (ms == null) return "—";
  const minutes = Math.floor(ms / 60000);
  if (minutes < 1) return "<1 мин";
  if (minutes < 60) return `${minutes} мин`;
  return `${Math.floor(minutes / 60)} ч ${String(minutes % 60).padStart(2, "0")} мин`;
}
