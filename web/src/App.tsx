// VFP: The terminal screen — tabs, connection indicators, the gaps workspace skeleton and the journal line.
// Changes when: a screen or a header indicator is added or its layout changes.
// Anti-goal:
// 1. Colour for anything but emergencies — sign and weight carry meaning, red marks only alarms.
// 2. Buttons that look active before their stage exists — unfinished actions are shown disabled with the reason.

import { useEffect, useState } from "react";
import { exchangeTitle, ExchangesSettings, signedMs } from "./Exchanges";
import { FeedScreen } from "./Feed";
import { clock, describe, uptime } from "./format";
import { PairsScreen } from "./Pairs";
import { StatsScreen } from "./Stats";
import { TradesScreen } from "./Trades";
import { useLive, type LinkState } from "./live";
import { apiGet } from "./session";
import type { ExchangeState, JournalEvent, Snapshot } from "./types";

type TabId = "gaps" | "funding" | "unlocks" | "trades" | "stats" | "pairs" | "settings";

const TABS: { id: TabId; label: string }[] = [
  { id: "gaps", label: "Гэпы" },
  { id: "funding", label: "Фандинг·скоро" },
  { id: "unlocks", label: "Разлоки·скоро" },
  { id: "trades", label: "Сделки" },
  { id: "stats", label: "Статистика" },
  { id: "pairs", label: "Пары" },
  { id: "settings", label: "Настройки" },
];

export function App({ token }: { token: string }) {
  const [tab, setTab] = useState<TabId>("gaps");
  const [history, setHistory] = useState<JournalEvent[]>([]);
  const [journalOpen, setJournalOpen] = useState(false);
  const live = useLive(token, history);
  const now = useNow(1000);

  useEffect(() => {
    apiGet<{ events: JournalEvent[] }>("/api/journal?limit=500", token)
      .then((data) => setHistory(data.events))
      .catch(() => undefined);
  }, [token]);

  if (live.link === "unauthorized") {
    return <SessionExpired />;
  }

  const openPairs = live.snapshot?.pairs.open ?? 0;
  const lastEvent = live.events[live.events.length - 1];

  return (
    <div className="screen">
      <header className="header">
        <nav className="tabs">
          {TABS.map((item) => (
            <button
              key={item.id}
              className={item.id === tab ? "tab active" : "tab"}
              onClick={() => setTab(item.id)}
            >
              {item.label}
            </button>
          ))}
        </nav>
        <div className="indicators">
          {live.snapshot?.exchanges.map((exchange) => <ExchangeIndicator key={exchange.name} exchange={exchange} />)}
          <DatabaseIndicator snapshot={live.snapshot} />
        </div>
      </header>

      <LinkBanner link={live.link} alarm={openPairs > 0} />

      <main className={live.link === "live" ? "body" : "body stale"}>
        {tab === "gaps" && <FeedScreen token={token} snapshot={live.snapshot} />}
        {(tab === "funding" || tab === "unlocks") && <Placeholder text="Раздел появится после MVP." />}
        {tab === "trades" && <TradesScreen token={token} />}
        {tab === "stats" && <StatsScreen token={token} recordedLive={live.snapshot?.history?.recorded} />}
        {tab === "pairs" && <PairsScreen token={token} summary={live.snapshot?.instruments} />}
        {tab === "settings" && <SettingsScreen token={token} snapshot={live.snapshot} now={now} />}
      </main>

      {journalOpen && <JournalPanel events={live.events} onClose={() => setJournalOpen(false)} />}

      <footer className="journal-line" onClick={() => setJournalOpen((open) => !open)}>
        <span className="muted">журнал:</span>{" "}
        {lastEvent ? (
          <span className={`level-${lastEvent.level}`}>
            {clock(lastEvent.ts_ms)} {describe(lastEvent)}
          </span>
        ) : (
          <span className="muted">событий пока нет</span>
        )}
      </footer>
    </div>
  );
}

const KEYS_NOTE: Record<ExchangeState["keys"], string | null> = {
  none: "нет ключей",
  saved: "ключ не проверен",
  checking: "проверка ключа…",
  ok: null,
  warning: null,
  rejected: "ключ не принят",
};

function ExchangeIndicator({ exchange }: { exchange: ExchangeState }) {
  const title = exchangeTitle(exchange);
  if (exchange.link === "down") {
    return <span className="indicator blink">○ {title} нет связи</span>;
  }
  if (exchange.link === "unknown") {
    return <span className="indicator muted">○ {title} …</span>;
  }
  const note = KEYS_NOTE[exchange.keys];
  return (
    <span className="indicator">
      ● {title} {exchange.ping_ms}мс
      {exchange.clock_warning && exchange.clock_offset_ms != null && (
        <span className="action"> · часы {signedMs(exchange.clock_offset_ms)}</span>
      )}
      {note && <span className={exchange.keys === "rejected" ? "action" : "muted"}> · {note}</span>}
    </span>
  );
}

function DatabaseIndicator({ snapshot }: { snapshot: Snapshot | null }) {
  if (!snapshot) return null;
  const { database } = snapshot;
  if (database.status === "ok") {
    return <span className="indicator muted">● БАЗА</span>;
  }
  return (
    <span className="indicator blink" title={database.error ?? ""}>
      ○ БАЗА нет связи · в буфере {database.spooled_rows + database.pending_rows}
    </span>
  );
}

function LinkBanner({ link, alarm }: { link: LinkState; alarm: boolean }) {
  if (link === "live") return null;
  const text =
    link === "connecting" ? "подключение к терминалу…" : "нет связи с терминалом · данные на экране устарели";
  return <div className={alarm ? "banner alarm blink" : "banner blink"}>{text}</div>;
}

function SettingsScreen({ token, snapshot, now }: { token: string; snapshot: Snapshot | null; now: number }) {
  return (
    <div className="settings">
      <ExchangesSettings token={token} live={snapshot?.exchanges} />
      <section>
        <h2>Торговля</h2>
        {snapshot?.trading ? (
          <>
            <div className="row">
              <span>торговля</span>
              <span className="strong">{snapshot.trading.enabled ? "ВКЛЮЧЕНА" : "выключена"}</span>
            </div>
            {Object.entries(snapshot.trading.settings).map(([key, value]) => (
              <div className="row" key={key}>
                <span>{key}</span>
                <span>{typeof value === "object" ? JSON.stringify(value) : String(value)}</span>
              </div>
            ))}
            <div className="row">
              <span>плечо и маржа выставлены для контрактов</span>
              <span>{snapshot.trading.warmed}</span>
            </div>
            <div className="row">
              <span>приватные потоки</span>
              <span>
                {Object.entries(snapshot.trading.private_streams ?? {})
                  .map(([name, on]) => `${name.toUpperCase()} ${on ? "на связи" : "нет"}`)
                  .join(" · ")}
              </span>
            </div>
            {snapshot.trading.warm_errors.map((error) => (
              <p key={`${error.exchange}${error.symbol}`} className="level-warning">
                {error.exchange.toUpperCase()} {error.symbol}: {error.error}
              </p>
            ))}
            <p className="muted">
              Включение, плечо и риск-лимиты задаются в config.toml, раздел [trading], и применяются после перезапуска. Размер на ногу
              и порог ленты меняются над лентой.
            </p>
          </>
        ) : (
          <p className="muted">загрузка…</p>
        )}
      </section>
      <section>
        <h2>Фильтры</h2>
        <p className="muted">Фильтры ленты — прямо над лентой на экране «Гэпы», запоминаются в этом браузере.</p>
      </section>
      <section>
        <h2>Уведомления</h2>
        <p className="muted">После MVP.</p>
      </section>
      {snapshot && (
        <section>
          <h2>Система</h2>
          <div className="row">
            <span>версия</span>
            <span>{snapshot.app.version}</span>
          </div>
          <div className="row">
            <span>работает</span>
            <span>{uptime(snapshot.app.started_ts_ms, now)}</span>
          </div>
          <div className="row">
            <span>база</span>
            <span>{snapshot.database.status === "ok" ? "подключена" : `нет связи: ${snapshot.database.error ?? ""}`}</span>
          </div>
          <div className="row">
            <span>строк в очереди / в буфере / отклонено</span>
            <span>
              {snapshot.database.pending_rows} / {snapshot.database.spooled_rows} / {snapshot.database.rejected_rows}
            </span>
          </div>
        </section>
      )}
    </div>
  );
}

function JournalPanel({ events, onClose }: { events: JournalEvent[]; onClose: () => void }) {
  return (
    <div className="journal-panel">
      <div className="toolbar">
        <span>ЖУРНАЛ · {events.length}</span>
        <button className="action" onClick={onClose}>
          [СВЕРНУТЬ]
        </button>
      </div>
      <ol className="journal-list">
        {[...events].reverse().map((event) => (
          <li key={`${event.ts_ms}-${event.source}-${event.type}`} className={`level-${event.level}`}>
            <span className="muted">{clock(event.ts_ms)}</span> {describe(event)}
          </li>
        ))}
      </ol>
    </div>
  );
}

function Placeholder({ text }: { text: string }) {
  return <p className="empty muted">{text}</p>;
}

function SessionExpired() {
  return (
    <div className="screen centered">
      <p>Сессия недействительна или терминал перезапущен.</p>
      <p className="muted">Запустите ludik.cmd — он откроет эту страницу с новым ключом сессии.</p>
    </div>
  );
}

function useNow(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs]);
  return now;
}
