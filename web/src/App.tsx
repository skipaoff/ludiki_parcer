// VFP: The terminal screen — tabs, connection indicators, the gaps workspace skeleton and the journal line.
// Changes when: a screen or a header indicator is added or its layout changes.
// Anti-goal:
// 1. Colour for anything but emergencies — sign and weight carry meaning, red marks only alarms.
// 2. Buttons that look active before their stage exists — unfinished actions are shown disabled with the reason.

import { useEffect, useState } from "react";
import { exchangeTitle, ExchangesSettings, signedMs } from "./Exchanges";
import { clock, describe, uptime } from "./format";
import { PairsScreen } from "./Pairs";
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
        {tab === "gaps" && <GapsScreen snapshot={live.snapshot} />}
        {(tab === "funding" || tab === "unlocks") && <Placeholder text="Раздел появится после MVP." />}
        {tab === "trades" && <Placeholder text="История закрытых пар появится на этапе 6." />}
        {tab === "stats" && <Placeholder text="Статистика появится на этапе 7. Запись истории вилок — этап 4." />}
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

function GapsScreen({ snapshot }: { snapshot: Snapshot | null }) {
  const pairs = snapshot?.pairs ?? { open: 0, limit: 0 };
  return (
    <div className="gaps">
      <section className="feed">
        <div className="toolbar muted">ROI ≥ —  ≤ —   Объём 24ч ≥ —   Размер —   сорт: ROI ↓</div>
        <table className="grid">
          <thead>
            <tr>
              <th className="left">МОНЕТА</th>
              <th className="left">ЛОНГ</th>
              <th className="left">ШОРТ</th>
              <th>ROI</th>
              <th>ЁМК.</th>
              <th>ЖИВЁТ</th>
              <th />
            </tr>
          </thead>
        </table>
        <p className="empty muted">Лента вилок появится на этапе 3, после подключения Binance и MEXC.</p>
      </section>
      <aside className="pairs">
        <div className="toolbar">
          <span>
            ОТКРЫТЫЕ ПАРЫ {pairs.open}/{pairs.limit}
          </span>
          <button className="action" disabled title="Торговля появится на этапе 6">
            [ЗАКРЫТЬ ВСЁ]
          </button>
        </div>
        <p className="empty muted">Открытых пар нет.</p>
      </aside>
    </div>
  );
}

function SettingsScreen({ token, snapshot, now }: { token: string; snapshot: Snapshot | null; now: number }) {
  return (
    <div className="settings">
      <ExchangesSettings token={token} live={snapshot?.exchanges} />
      <section>
        <h2>Торговля</h2>
        <p className="muted">Размер, плечо, пороги и риск-лимиты — этапы 3 и 6.</p>
      </section>
      <section>
        <h2>Фильтры</h2>
        <p className="muted">Фильтры ленты — этап 3.</p>
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
