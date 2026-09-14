// VFP: The Gaps screen — the live feed of gaps with book-based ROI, filters, the radar of best current spreads and the open-pairs column.
// Changes when: feed columns, row states, filters or the radar presentation change (PLAN.md, section 3.1).
// Anti-goal:
// 1. An active Open button before trading exists — it stays disabled with the reason until stage 6.
// 2. Hiding why a row is grey — every blocked row carries its reason on hover and in the status column.
// 3. Recomputing trading numbers in the browser — the terminal computes, the screen only filters and sorts.

import { useEffect, useMemo, useState } from "react";
import { compact } from "./Pairs";
import { PortfolioColumn } from "./Portfolio";
import { apiSend } from "./session";
import { explainFailure, reasonText, tradingAction } from "./trading";
import type { FeedRow, FeedView, Snapshot } from "./types";

type SortKey = "roi" | "capacity" | "age";

interface Filters {
  roiMax: string;
  volumeMin: string;
  capacityMin: string;
  showSuspicious: boolean;
  showReadOnly: boolean;
  longExchange: string;
  shortExchange: string;
  search: string;
  sort: SortKey;
}

const DEFAULT_FILTERS: Filters = {
  roiMax: "15",
  volumeMin: "1000000",
  capacityMin: "",
  showSuspicious: false,
  showReadOnly: true,
  longExchange: "",
  shortExchange: "",
  search: "",
  sort: "roi",
};

const READ_ONLY = new Set(["variational"]);

const BLOCK_TEXT: Record<string, string> = {
  stale: "данные устарели",
  no_book: "нет стакана",
  book_too_thin: "глубины не хватает на размер",
  suspicious: "подозрительная пара",
  blacklisted: "чёрный список",
  size_below_common_step: "размер меньше шага количества",
  non_positive_input: "нет цены",
};

export function blockText(block: string | null): string | null {
  if (!block) return null;
  const [code, exchange] = block.split(":");
  const base: Record<string, string> = {
    below_min_qty: "меньше минимального количества",
    below_min_notional: "меньше минимальной суммы ордера",
    above_max_market_qty: "больше максимума рыночного ордера",
  };
  if (base[code]) return `${base[code]} · ${exchange?.toUpperCase()}`;
  return BLOCK_TEXT[block] ?? block;
}

function loadFilters(): Filters {
  try {
    return { ...DEFAULT_FILTERS, ...JSON.parse(localStorage.getItem("ludik.feed.filters") ?? "{}") };
  } catch {
    return DEFAULT_FILTERS;
  }
}

function lifetime(ms: number | null): string {
  if (ms === null) return "—";
  const seconds = Math.floor(ms / 1000);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = String(seconds % 60).padStart(2, "0");
  return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${rest}` : `${minutes}:${rest}`;
}

function signedPct(value: string | number | null, digits = 2): string {
  if (value === null) return "—";
  const number = Number(value);
  const sign = number > 0 ? "+" : number < 0 ? "−" : "";
  return `${sign}${Math.abs(number).toFixed(digits)}%`;
}

function price(value: string | null): string {
  if (value === null) return "—";
  const number = Number(value);
  if (number >= 1) return number.toLocaleString("en-US", { maximumFractionDigits: 4 });
  return number.toFixed(Math.min(15, 4 - Math.floor(Math.log10(number))));
}

function legText(leg: FeedRow["long"], avg: string | null): string {
  return leg ? `${leg.exchange.toUpperCase()} ${price(avg)}` : "—";
}

function SettingsForm({
  token,
  sizeUsd,
  minRoiPct,
  enterAfterMs,
}: {
  token: string;
  sizeUsd?: string;
  minRoiPct?: string;
  enterAfterMs?: number;
}) {
  const [size, setSize] = useState("");
  const [threshold, setThreshold] = useState("");
  const [lifetime, setLifetime] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const lifetimeSeconds = enterAfterMs === undefined ? "" : String(enterAfterMs / 1000);

  const dirty =
    (size !== "" && size !== sizeUsd) || (threshold !== "" && threshold !== minRoiPct) || (lifetime !== "" && lifetime !== lifetimeSeconds);

  const apply = async () => {
    const body: Record<string, string> = {};
    if (size !== "" && size !== sizeUsd) body.size_usd = size;
    if (threshold !== "" && threshold !== minRoiPct) body.min_roi_pct = threshold;
    if (lifetime !== "" && lifetime !== lifetimeSeconds) {
      const seconds = Number(lifetime.replace(",", "."));
      if (!Number.isFinite(seconds) || seconds < 0) {
        setMessage("мин. жизнь — число секунд");
        return;
      }
      body.enter_after_ms = String(Math.round(seconds * 1000));
    }
    setBusy(true);
    setMessage(null);
    try {
      await apiSend("PUT", "/api/feed/settings", token, body);
      setSize("");
      setThreshold("");
      setLifetime("");
    } catch (reason) {
      setMessage((reason as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      className="settings-form"
      onSubmit={(event) => {
        event.preventDefault();
        void apply();
      }}
    >
      Размер $<input value={size === "" ? sizeUsd ?? "" : size} onChange={(event) => setSize(event.target.value)} /> на ногу ·
      порог ленты <input value={threshold === "" ? minRoiPct ?? "" : threshold} onChange={(event) => setThreshold(event.target.value)} />% ·
      живёт ≥ <input value={lifetime === "" ? lifetimeSeconds : lifetime} onChange={(event) => setLifetime(event.target.value)} />с{" "}
      {dirty && (
        <button className="action" type="submit" disabled={busy}>
          [ПРИМЕНИТЬ]
        </button>
      )}
      {message && <span className="level-warning"> {message}</span>}
    </form>
  );
}

function OpenButton({ token, row, onResult }: { token: string; row: FeedRow; onResult: (message: string | null) => void }) {
  const [sending, setSending] = useState(false);
  const blocks = row.open_blocks ?? ["trading_disabled"];
  const blocked = blocks.length > 0 || sending;
  const open = async () => {
    setSending(true);
    onResult(null);
    try {
      const card = await tradingAction<{ status: string; token: string }>("/api/trading/open", token, { pair_key: row.key });
      onResult(`✓ ${card.token}: ${card.status === "open" ? "пара открыта" : `статус ${card.status}`}`);
    } catch (error) {
      onResult(`${row.token}: ${explainFailure(error)}`);
    } finally {
      setSending(false);
    }
  };
  return (
    <button className="action" disabled={blocked} title={blocks.map(reasonText).join("; ") || "открыть пару рыночными ордерами"} onClick={() => void open()}>
      {sending ? "[…]" : "[ОТКРЫТЬ]"}
    </button>
  );
}

export function FeedScreen({ token, snapshot }: { token: string; snapshot: Snapshot | null }) {
  const [filters, setFilters] = useState<Filters>(loadFilters);
  const [tradeMessage, setTradeMessage] = useState<string | null>(null);
  const feed: FeedView | undefined = snapshot?.feed;

  useEffect(() => {
    try {
      localStorage.setItem("ludik.feed.filters", JSON.stringify(filters));
    } catch {
      // storage may be unavailable
    }
  }, [filters]);

  const set = <K extends keyof Filters>(key: K, value: Filters[K]) => setFilters((current) => ({ ...current, [key]: value }));

  const rows = useMemo(() => {
    const number = (value: string, fallback: number) => (value.trim() === "" ? fallback : Number(value));
    const roiMax = number(filters.roiMax, Infinity);
    const volumeMin = number(filters.volumeMin, 0);
    const capacityMin = number(filters.capacityMin, 0);
    const query = filters.search.trim().toUpperCase();
    const visible = (feed?.rows ?? []).filter((row) => {
      // The minimum is the terminal's feed threshold; only the maximum (fake gaps) is filtered here.
      if (Number(row.roi_net_pct ?? -Infinity) > roiMax) return false;
      if (row.volume24h_weak_usd !== null && Number(row.volume24h_weak_usd) < volumeMin) return false;
      if (capacityMin && Number(row.capacity_usd ?? 0) < capacityMin) return false;
      if (!filters.showSuspicious && row.suspicious) return false;
      if (filters.longExchange && row.long?.exchange !== filters.longExchange) return false;
      if (filters.shortExchange && row.short?.exchange !== filters.shortExchange) return false;
      if (!filters.showReadOnly && (READ_ONLY.has(row.long?.exchange ?? "") || READ_ONLY.has(row.short?.exchange ?? ""))) return false;
      if (query && !row.token.includes(query)) return false;
      return true;
    });
    const key = (row: FeedRow) =>
      filters.sort === "capacity" ? Number(row.capacity_usd ?? 0) : filters.sort === "age" ? row.lifetime_ms ?? 0 : Number(row.roi_net_pct ?? -1e9);
    return visible.sort((x, y) => key(y) - key(x));
  }, [feed, filters]);

  const settings = feed?.settings;
  const stats = feed?.stats ?? {};
  const streams = feed?.streams;
  const exchanges = (snapshot?.exchanges ?? []).map((exchange) => exchange.name);

  return (
    <div className="gaps">
      <section className="feed">
        <div className="toolbar filters feed-filters">
          <label>
            ROI ≤ <input value={filters.roiMax} onChange={(event) => set("roiMax", event.target.value)} />%
          </label>
          <label>
            Объём 24ч ≥ $ <input value={filters.volumeMin} onChange={(event) => set("volumeMin", event.target.value)} />
          </label>
          <label>
            Ёмк. ≥ $ <input value={filters.capacityMin} onChange={(event) => set("capacityMin", event.target.value)} />
          </label>
          <label className="check-label">
            <input type="checkbox" checked={filters.showSuspicious} onChange={(event) => set("showSuspicious", event.target.checked)} />{" "}
            подозрит.
          </label>
          <label className="check-label" title="Variational: торгового API нет, вилки только для наблюдения">
            <input type="checkbox" checked={filters.showReadOnly} onChange={(event) => set("showReadOnly", event.target.checked)} />{" "}
            только наблюдение
          </label>
          <select value={filters.longExchange} onChange={(event) => set("longExchange", event.target.value)}>
            <option value="">лонг: все</option>
            {exchanges.map((name) => (
              <option key={name} value={name}>
                лонг {name.toUpperCase()}
              </option>
            ))}
          </select>
          <select value={filters.shortExchange} onChange={(event) => set("shortExchange", event.target.value)}>
            <option value="">шорт: все</option>
            {exchanges.map((name) => (
              <option key={name} value={name}>
                шорт {name.toUpperCase()}
              </option>
            ))}
          </select>
          <input placeholder="монета" value={filters.search} onChange={(event) => set("search", event.target.value)} />
        </div>
        <div className="toolbar muted">
          <SettingsForm token={token} sizeUsd={settings?.size_usd} minRoiPct={settings?.min_roi_pct} enterAfterMs={settings?.enter_after_ms} />
          <span>
            тейкер{" "}
            {Object.entries(settings?.taker_fee_pct ?? {})
              .map(([name, fee]) => `${name.toUpperCase()} ${fee}%`)
              .join(" · ") || "—"}
          </span>
          <span>
            сорт:{" "}
            {(["roi", "capacity", "age"] as SortKey[]).map((value) => (
              <button key={value} className={filters.sort === value ? "tab active" : "tab"} onClick={() => set("sort", value)}>
                {value === "roi" ? "ROI" : value === "capacity" ? "ёмк." : "живёт"}
              </button>
            ))}
          </span>
        </div>

        <table className="grid feed-table">
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
          <tbody>
            {rows.map((row) => {
              const reason = blockText(row.block);
              return (
                <tr key={row.key} className={reason ? "muted" : ""} title={reason ?? undefined}>
                  <td className="left strong">
                    {row.token}
                    {row.suspicious ? " ?" : ""}
                  </td>
                  <td className="left">{legText(row.long, row.long_avg)}</td>
                  <td className="left">{legText(row.short, row.short_avg)}</td>
                  <td className="strong" title={`до комиссий ${signedPct(row.roi_gross_pct)}`}>
                    {signedPct(row.roi_net_pct)}
                  </td>
                  <td>${compact(row.capacity_usd)}</td>
                  <td>{lifetime(row.lifetime_ms)}</td>
                  <td>
                    <OpenButton token={token} row={row} onResult={setTradeMessage} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {rows.length === 0 && (
          <p className="empty muted">
            {feed
              ? `Сейчас нет вилок, которые держатся выше порога дольше ${Math.round((settings?.enter_after_ms ?? 0) / 1000)} с. Кандидаты и лучшие текущие спреды — в радаре ниже.`
              : "Лента запускается…"}
          </p>
        )}
        {tradeMessage && (
          <p className={tradeMessage.startsWith("✓") ? "empty" : "empty level-warning"} onClick={() => setTradeMessage(null)}>
            {tradeMessage}
          </p>
        )}
        {snapshot?.trading && !snapshot.trading.enabled && (
          <p className="empty muted">Торговля выключена: включается в config.toml, раздел [trading], после пробной сделки.</p>
        )}

        <Radar rows={feed?.radar ?? []} feeTotals={settings?.taker_fee_pct ?? {}} enterAfterMs={settings?.enter_after_ms} />

        <div className="toolbar muted stats-line">
          пар {stats.pairs ?? "—"} · в радаре {stats.radar_pairs ?? "—"} · стаканов {stats.books ?? "—"} · отслеживается{" "}
          {stats.tracked ?? "—"} · вилок с запуска {stats.gaps_entered ?? "—"} · такт {stats.tick_ms ?? "—"}мс · лаг{" "}
          {stats.loop_lag_ms ?? "—"}мс
          {streams &&
            Object.entries(streams).map(([name, info]) => (
              <span key={name}>
                {" "}
                · {name.toUpperCase()}
                {info.sockets !== undefined ? ` соединений ${info.connections}/${info.sockets}` : ""}
                {info.depth_symbols !== undefined ? ` стаканов ${info.depth_symbols}` : ""}
                {info.polls !== undefined ? ` опросов ${info.polls}${info.poll_errors ? ` (ошибок ${info.poll_errors})` : ""}` : ""}
                {info.listings !== undefined ? ` рынков ${info.listings}` : ""}
              </span>
            ))}
        </div>
      </section>
      <PortfolioColumn token={token} snapshot={snapshot} />
    </div>
  );
}

function radarStatus(row: FeedRow, enterAfterMs: number | undefined): string {
  const blocked = blockText(row.block);
  if (blocked) return blocked;
  if (row.phase === "candidate" && row.lifetime_ms !== null && enterAfterMs) {
    return `живёт ${Math.floor(row.lifetime_ms / 1000)}/${Math.round(enterAfterMs / 1000)}с`;
  }
  return row.phase === "tracking" ? "отслеживается" : "ок";
}

function Radar({ rows, feeTotals, enterAfterMs }: { rows: FeedRow[]; feeTotals: Record<string, string>; enterAfterMs?: number }) {
  if (rows.length === 0) return null;
  const roundTrip = (row: FeedRow) => {
    if (!row.long || !row.short) return null;
    const long = Number(feeTotals[row.long.exchange] ?? NaN);
    const short = Number(feeTotals[row.short.exchange] ?? NaN);
    return Number.isNaN(long + short) ? null : (long + short) * 2;
  };
  return (
    <>
      <div className="toolbar section-title">
        <span>РАДАР · лучшие спреды сейчас, ниже порога ленты</span>
      </div>
      <p className="empty muted legend">
        Лучш. цены, стакан и выход — одна величина: разница цен шорт − лонг в % от цены лонга, без комиссий. Лучш. цены — по
        первым уровням, стакан — по средней цене на ваш размер, выход — сколько стоит закрыть тот же размер сейчас. ROI =
        стакан − комиссии круга (4 тейкера).
      </p>
      <table className="grid feed-table radar-table">
        <thead>
          <tr>
            <th className="left">МОНЕТА</th>
            <th className="left">ЛОНГ → ШОРТ</th>
            <th title="разница цен шорт − лонг по лучшим ценам, без комиссий">ЛУЧШ. ЦЕНЫ</th>
            <th title="разница средних цен шорт − лонг по стакану на размер, без комиссий">СТАКАН</th>
            <th title="разница цен шорт − лонг при закрытии размера сейчас, без комиссий">ВЫХОД</th>
            <th title="комиссии круга: вход и выход на обеих ногах">КОМИССИИ</th>
            <th title="по стакану на размер после комиссий круга">ROI</th>
            <th>ВОЗРАСТ</th>
            <th className="left">СТАТУС</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const fees = roundTrip(row);
            return (
              <tr key={row.key} className="muted">
                <td className="left">{row.token}</td>
                <td className="left">
                  {row.long && row.short ? `${row.long.exchange.toUpperCase()} → ${row.short.exchange.toUpperCase()}` : "—"}
                </td>
                <td>{signedPct(row.top_gross_pct)}</td>
                <td>{signedPct(row.roi_gross_pct)}</td>
                <td>{signedPct(row.exit_spread_pct)}</td>
                <td>{fees === null ? "—" : `${fees.toFixed(2)}%`}</td>
                <td className="strong">{signedPct(row.roi_net_pct)}</td>
                <td>
                  {row.age_long_ms ?? "—"}/{row.age_short_ms ?? "—"}мс
                </td>
                <td className="left">{radarStatus(row, enterAfterMs)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </>
  );
}
