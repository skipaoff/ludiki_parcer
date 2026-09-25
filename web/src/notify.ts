// VFP: Turning the terminal's events into desktop notifications, so a gap is noticed while the tab is in the background.
// Changes when: what the user is told about, or how, changes.
// Anti-goal:
// 1. Announcing the past — events older than this tab are history replay, not news.
// 2. Asking for permission on load: browsers grant it only on a click, and a refused prompt cannot be asked again.
// 3. A second source of truth about what is worth announcing — the terminal decides, the browser only shows.

import type { JournalEvent } from "./types";

const STORAGE_KEY = "ludik.notify";
const FRESH_MS = 30_000;
/** A notification for one pair is replaced, not stacked: the newest numbers are the ones worth reading. */
const TAG = "ludik-gap";

export interface NotifyPrefs {
  gaps: boolean;
  alarms: boolean;
  minInterest: number;
  onlyHidden: boolean;
  sound: boolean;
}

export const DEFAULT_PREFS: NotifyPrefs = { gaps: true, alarms: true, minInterest: 0, onlyHidden: false, sound: false };

export function loadPrefs(): NotifyPrefs {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    return stored ? { ...DEFAULT_PREFS, ...(JSON.parse(stored) as Partial<NotifyPrefs>) } : DEFAULT_PREFS;
  } catch {
    return DEFAULT_PREFS;
  }
}

export function savePrefs(prefs: NotifyPrefs): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
  } catch {
    // a browser that refuses storage still notifies, it just forgets the settings
  }
}

export type Permission = NotificationPermission | "unsupported";

export function permission(): Permission {
  return typeof Notification === "undefined" ? "unsupported" : Notification.permission;
}

export async function askPermission(): Promise<Permission> {
  if (typeof Notification === "undefined") return "unsupported";
  return await Notification.requestPermission();
}

export interface Announcement {
  title: string;
  body: string;
  tag: string;
}

/** What to show for one event, or null when it is not worth interrupting for. Pure: the caller shows it. */
export function announcement(event: JournalEvent, prefs: NotifyPrefs, now: number, hidden: boolean): Announcement | null {
  if (now - event.ts_ms > FRESH_MS) return null;
  if (prefs.onlyHidden && !hidden) return null;
  if (event.level === "critical" && prefs.alarms) {
    return { title: "Терминал: авария", body: alarmText(event), tag: `ludik-alarm-${event.type}` };
  }
  if (event.type !== "gap_actionable" || !prefs.gaps) return null;
  const interest = Number(event.payload.interest ?? 0);
  if (Number.isFinite(interest) && interest < prefs.minInterest) return null;
  const token = String(event.payload.token ?? "?");
  const long = String(event.payload.long ?? "?").toUpperCase();
  const short = String(event.payload.short ?? "?").toUpperCase();
  const total = event.payload.total_pct == null ? "—" : `${event.payload.total_pct}%`;
  const size = event.payload.size_usd == null ? "" : ` на $${event.payload.size_usd}`;
  const blocked = Array.isArray(event.payload.blocks) && event.payload.blocks.length > 0 ? " · открыть нельзя: торговля выключена" : "";
  return {
    title: `${token}: итог ${total}`,
    body: `лонг ${long}, шорт ${short}${size} · интерес ${interest || "—"}${blocked}`,
    tag: `${TAG}-${String(event.payload.key ?? token)}`,
  };
}

function alarmText(event: JournalEvent): string {
  const token = event.payload.token ? `${String(event.payload.token)}: ` : "";
  return `${token}${event.type.replace(/_/g, " ")}`;
}

export function show(announce: Announcement, prefs: NotifyPrefs): void {
  if (permission() !== "granted") return;
  const notification = new Notification(announce.title, { body: announce.body, tag: announce.tag });
  notification.onclick = () => {
    window.focus();
    notification.close();
  };
  if (prefs.sound) beep();
}

function beep(): void {
  try {
    const audio = new AudioContext();
    const oscillator = audio.createOscillator();
    const gain = audio.createGain();
    oscillator.frequency.value = 880;
    gain.gain.value = 0.05;
    oscillator.connect(gain).connect(audio.destination);
    oscillator.start();
    oscillator.stop(audio.currentTime + 0.12);
    oscillator.onended = () => void audio.close();
  } catch {
    // no audio device, or the browser wants a gesture first: the notification itself is still shown
  }
}
