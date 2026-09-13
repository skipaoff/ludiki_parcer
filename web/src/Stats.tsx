// VFP: The Statistics screen, stage 4 edition — what recorded history already says about gaps: funnel, lifetimes, peaks, missed profit, storage growth.
// Changes when: recorded history gains new summaries; the full statistics screen of stage 7 replaces the preview parts.
// Anti-goal:
// 1. Computing statistics in the browser — the database aggregates, the screen shows.

import { useCallback, useEffect, useState } from "react";
import { clock } from "./format";
import { compact } from "./Pairs";
import { apiGet } from "./session";

interface Summary {
  hours: number;
  gaps: number;
  opened: number;
  live: number;
  converged: number;
  suspicious: number;
  median_in_feed_s: number | null;
  median_to_converge_s: number | null;
  best_roi_peak: string | null;
  avg_roi_peak: string | null;
  avg_missed_pnl_pct: string | null;
  missed_pnl_usd: string | null;
  tokens: number;
  roi_buckets: { bucket: number; gaps: number; avg_in_feed_s: number | null }[];
}

interface Episode {
  id: string;
  token: string;
  long_exchange: string;
  short_exchange: string;
  size_usd: string;
  entered_feed_at: number;
  left_feed_at: number | null;
  ended_at: number | null;
  end_reason: string | null;
  roi_first: string | null;
  roi_peak: string | null;
  capacity_peak_usd: string | null;
  missed_pnl_best_pct: string | null;
  samples_count: number;
  suspicious: boolean;
}

interface Storage {
  database_bytes: number;
  episodes_24h: number;
  hypertables: Record<string, { bytes: number; rows_24h: number }>;
}

const BUCKETS = ["< 0.5%", "0.5–1%", "1–2%", "2–5%", "≥ 5%"];
const END_REASON: Record<string, string> = {
  converged: "сошлась",
  timeout: "24 ч",
  delisted: "делистинг",
  evicted: "вытеснена",
  app_stop: "остановка",
};

function pct(value: string | null, digits = 2): string {
  return value === null ? "—" : `${Number(value).toFixed(digits)}%`;
}

function duration(seconds: number | null): string {
  if (seconds === null) return "—";
  const total = Math.round(seconds);
  if (total < 60) return `${total}с`;
  if (total < 3600) return `${Math.floor(total / 60)}м ${total % 60}с`;
  return `${Math.floor(total / 3600)}ч ${Math.floor((total % 3600) / 60)}м`;
}

function megabytes(bytes: number | null | undefined): string {
  return bytes == null ? "—" : `${(bytes / 1024 / 1024).toFixed(1)} МБ`;
}

export function StatsScreen({ token, recordedLive }: { token: string; recordedLive: number | undefined }) {
  const [hours, setHours] = useState(24);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [episodes, setEpisodes] = useState<Episode[]>([]);
  const [storage, setStorage] = useState<Storage | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    Promise.all([
      apiGet<Summary>(`/api/history/summary?hours=${hours}`, token),
      apiGet<{ episodes: Episode[] }>("/api/history/episodes?limit=100", token),
      apiGet<Storage>("/api/history/storage", token),
    ])
      .then(([nextSummary, nextEpisodes, nextStorage]) => {
        setSummary(nextSummary);
        setEpisodes(nextEpisodes.episodes);
        setStorage(nextStorage);
        setError(null);
      })
      .catch((reason: Error) => setError(reason.message));
  }, [hours, token]);

  useEffect(() => {
    load();
    const timer = window.setInterval(load, 10_000);
    return () => window.clearInterval(timer);
  }, [load, recordedLive]);

  return (
    <div className="settings stats">
      <div className="toolbar filters">
        <span>ИСТОРИЯ ВИЛОК</span>
        {[1, 24, 168, 720].map((value) => (
          <button key={value} className={hours === value ? "tab active" : "tab"} onClick={() => setHours(value)}>
            {value === 1 ? "час" : value === 24 ? "сутки" : value === 168 ? "неделя" : "месяц"}
          </button>
        ))}
      </div>
      {error && <p className="level-warning">{error}</p>}
      {summary && (
        <>
          <section>
            <h2>Воронка и время жизни</h2>
            <div className="row"><span>вилок в ленте</span><span>{summary.gaps} · монет {summary.tokens}</span></div>
            <div className="row"><span>открыто</span><span>{summary.opened}</span></div>
            <div className="row"><span>сейчас отслеживается</span><span>{summary.live}</span></div>
            <div className="row"><span>сошлись / подозрительных</span><span>{summary.converged} / {summary.suspicious}</span></div>
            <div className="row"><span>медиана времени в ленте</span><span>{duration(summary.median_in_feed_s)}</span></div>
            <div className="row"><span>медиана до схождения цен</span><span>{duration(summary.median_to_converge_s)}</span></div>
          </section>
          <section>
            <h2>ROI и упущенная прибыль</h2>
            <div className="row"><span>лучший пик ROI / средний пик</span><span>{pct(summary.best_roi_peak)} / {pct(summary.avg_roi_peak)}</span></div>
            <div className="row">
              <span>упущенная прибыль: в среднем на вилку / всего при размере из настроек</span>
              <span>{pct(summary.avg_missed_pnl_pct)} / ${compact(summary.missed_pnl_usd)}</span>
            </div>
            <table className="grid feed-table">
              <thead>
                <tr><th className="left">ПИК ROI</th><th>ВИЛОК</th><th>СРЕДНЕЕ ВРЕМЯ В ЛЕНТЕ</th></tr>
              </thead>
              <tbody>
                {summary.roi_buckets.map((bucket) => (
                  <tr key={bucket.bucket}>
                    <td className="left">{BUCKETS[bucket.bucket] ?? bucket.bucket}</td>
                    <td>{bucket.gaps}</td>
                    <td>{duration(bucket.avg_in_feed_s)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        </>
      )}
      <section>
        <h2>Последние вилки</h2>
        {episodes.length === 0 ? (
          <p className="muted">Пока ни одна вилка не дошла до ленты. Запись идёт, пока терминал запущен.</p>
        ) : (
          <div className="table-scroll">
            <table className="grid feed-table">
              <thead>
                <tr>
                  <th className="left">ВХОД</th>
                  <th className="left">МОНЕТА</th>
                  <th className="left">ЛОНГ → ШОРТ</th>
                  <th>ROI ВХОД</th>
                  <th>ПИК</th>
                  <th>ЁМК. ПИК</th>
                  <th>В ЛЕНТЕ</th>
                  <th>УПУЩЕНО</th>
                  <th className="left">ИТОГ</th>
                </tr>
              </thead>
              <tbody>
                {episodes.map((episode) => (
                  <tr key={episode.id} className={episode.suspicious ? "muted" : ""}>
                    <td className="left">{clock(episode.entered_feed_at)}</td>
                    <td className="left strong">{episode.token}</td>
                    <td className="left">
                      {episode.long_exchange.toUpperCase()} → {episode.short_exchange.toUpperCase()}
                    </td>
                    <td>{pct(episode.roi_first)}</td>
                    <td>{pct(episode.roi_peak)}</td>
                    <td>${compact(episode.capacity_peak_usd)}</td>
                    <td>
                      {duration(
                        ((episode.left_feed_at ?? episode.ended_at ?? Date.now()) - episode.entered_feed_at) / 1000,
                      )}
                    </td>
                    <td>{pct(episode.missed_pnl_best_pct)}</td>
                    <td className="left">{episode.ended_at ? END_REASON[episode.end_reason ?? ""] ?? episode.end_reason : "идёт"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      {storage && (
        <section>
          <h2>Объём базы</h2>
          <div className="row"><span>вся база</span><span>{megabytes(storage.database_bytes)}</span></div>
          <div className="row"><span>вилок за сутки</span><span>{storage.episodes_24h}</span></div>
          {Object.entries(storage.hypertables).map(([table, info]) => (
            <div className="row" key={table}>
              <span>{table}: строк за сутки</span>
              <span>
                {info.rows_24h.toLocaleString("ru-RU")} · {megabytes(info.bytes)}
              </span>
            </div>
          ))}
        </section>
      )}
    </div>
  );
}
