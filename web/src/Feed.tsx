// VFP: The Gaps screen — one line per coin with its most interesting pair (long, short, profit on the size after book and fees, funding, expected result, interest), the coin's other pairs on click, the radar below the feed and the open-pairs column.
// Changes when: feed columns, grouping, row states, filters or the radar presentation change (PLAN.md, section 3.1).
// Anti-goal:
// 1. Recomputing trading numbers in the browser — the terminal computes; the screen filters, groups and sorts.
// 2. Numbers that flicker without meaning — book ages and per-second timers stay off the table.
// 3. Hiding why a line cannot be opened — every status has its explanation on hover.

import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { rateText, settlementTimers, signClass, signedPct, signedUsd } from "./money";
import { PortfolioColumn } from "./Portfolio";
import { apiSend } from "./session";
import { explainFailure, reasonText, tradingAction } from "./trading";
import type { FeedRow, FeedView, Snapshot } from "./types";

type SortKey = "total" | "profit";

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
  sort: "total",
};

const READ_ONLY = new Set(["variational"]);
const SOON_MS = 60 * 60 * 1000;

function loadFilters(): Filters {
  try {
    const stored = { ...DEFAULT_FILTERS, ...JSON.parse(localStorage.getItem("ludik.feed.filters") ?? "{}") };
    // "score" was the interest column, which is gone; a stored one falls back to the expected result.
    if (!["total", "profit"].includes(stored.sort)) stored.sort = "total";
    return stored;
  } catch {
    return DEFAULT_FILTERS;
  }
}

function rank(row: FeedRow, sort: SortKey): number {
  if (sort === "profit") return row.roi_net_pct === null ? -1e9 : Number(row.roi_net_pct);
  return row.total_pct === null ? -1e9 : Number(row.total_pct);
}

interface Group {
  token: string;
  head: FeedRow;
  others: FeedRow[];
}

function groupByToken(rows: FeedRow[], sort: SortKey, headCandidates: (row: FeedRow) => boolean = () => true): Group[] {
  const byToken = new Map<string, FeedRow[]>();
  for (const row of rows) {
    byToken.set(row.token, [...(byToken.get(row.token) ?? []), row]);
  }
  const groups: Group[] = [];
  for (const [token, list] of byToken) {
    const ordered = [...list].sort((x, y) => rank(y, sort) - rank(x, sort));
    const head = ordered.find(headCandidates);
    if (!head) continue;
    groups.push({ token, head, others: ordered.filter((row) => row !== head) });
  }
  return groups.sort((x, y) => rank(y.head, sort) - rank(x.head, sort));
}

function SettingsForm({
  token,
  sizeUsd,
  minRoiPct,
  enterAfterMs,
  horizonH,
}: {
  token: string;
  sizeUsd?: string;
  minRoiPct?: string;
  enterAfterMs?: number;
  horizonH?: string;
}) {
  const [size, setSize] = useState("");
  const [threshold, setThreshold] = useState("");
  const [lifetime, setLifetime] = useState("");
  const [horizon, setHorizon] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const lifetimeSeconds = enterAfterMs === undefined ? "" : String(enterAfterMs / 1000);

  const changed = (value: string, current: string | undefined) => value !== "" && value !== current;
  const dirty = changed(size, sizeUsd) || changed(threshold, minRoiPct) || changed(lifetime, lifetimeSeconds) || changed(horizon, horizonH);

  const apply = async () => {
    const body: Record<string, string> = {};
    if (changed(size, sizeUsd)) body.size_usd = size;
    if (changed(threshold, minRoiPct)) body.min_roi_pct = threshold;
    if (changed(horizon, horizonH)) body.funding_horizon_h = horizon.replace(",", ".");
    if (changed(lifetime, lifetimeSeconds)) {
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
      setHorizon("");
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
      Размер $<input value={size === "" ? sizeUsd ?? "" : size} onChange={(event) => setSize(event.target.value)} /> на ногу · порог
      ленты <input value={threshold === "" ? minRoiPct ?? "" : threshold} onChange={(event) => setThreshold(event.target.value)} />% ·
      живёт ≥ <input value={lifetime === "" ? lifetimeSeconds : lifetime} onChange={(event) => setLifetime(event.target.value)} />с ·
      фандинг за <input value={horizon === "" ? horizonH ?? "" : horizon} onChange={(event) => setHorizon(event.target.value)} />ч{" "}
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

function Leg({ leg }: { leg: FeedRow["long"] }) {
  if (!leg) return <>—</>;
  if (!leg.url) return <>{leg.exchange.toUpperCase()}</>;
  return (
    <a href={leg.url} target="_blank" rel="noreferrer">
      {leg.exchange.toUpperCase()}
    </a>
  );
}

function FundingCell({ row, horizonH, now }: { row: FeedRow; horizonH: string; now: number }) {
  const funding = row.funding;
  if (!funding || funding.horizon_pct === null) {
    return <span className="muted" title="ставка фандинга одной из бирж не получена">?</span>;
  }
  const soon = funding.next_ms !== null && funding.next_pct !== null && funding.next_ms - now < SOON_MS;
  const hint = [
    `За ${horizonH} ч пара ${Number(funding.horizon_pct) >= 0 ? "получит" : "заплатит"} ${signedPct(funding.horizon_pct, 3).replace("−", "")} от размера.`,
    `Лонг ${rateText(funding.long)}, шорт ${rateText(funding.short)}.`,
    `Расчёт: ${settlementTimers(funding.long, funding.short, now)} (лонг/шорт).`,
    "Лонг платит при положительной ставке своей биржи, шорт получает; при отрицательной — наоборот.",
  ].join(" ");
  return (
    <span title={hint}>
      <span className={signClass(funding.horizon_pct)}>{signedPct(funding.horizon_pct)}</span>
      <span className="muted"> {settlementTimers(funding.long, funding.short, now)}</span>
      {soon && Number(funding.next_pct) <= -0.005 && <span className="loss"> {signedPct(funding.next_pct)}</span>}
    </span>
  );
}

function GapLine({
  row,
  others,
  expanded,
  nested,
  onToggle,
  token,
  horizonH,
  sizeUsd,
  now,
  onResult,
}: {
  row: FeedRow;
  others: number;
  expanded: boolean;
  nested: boolean;
  onToggle: () => void;
  token: string;
  horizonH: string;
  sizeUsd: string;
  now: number;
  onResult: (message: string | null) => void;
}) {
  const dim = row.total_pct === null;
  return (
    <tr className={dim ? "muted" : ""}>
      <td className="left strong">
        {nested ? (
          <span className="muted">└</span>
        ) : others > 0 ? (
          <button className="coin" onClick={onToggle} title={expanded ? "скрыть другие пары" : `ещё пар по монете: ${others}`}>
            {row.token} {expanded ? "▾" : "▸"}
            <span className="muted">{others}</span>
          </button>
        ) : (
          row.token
        )}
        {row.suspicious ? " ?" : ""}
      </td>
      <td className="left">
        <Leg leg={row.long} />
      </td>
      <td className="left">
        <Leg leg={row.short} />
      </td>
      <td
        className="strong"
        title={`Прибыль на $${sizeUsd}, если цены сойдутся: разница средних цен по стакану минус комиссии входа и выхода на обеих биржах.`}
      >
        {row.profit_usd === null ? "—" : signedUsd(row.profit_usd)}
        {row.roi_net_pct !== null && <span className="muted"> {signedPct(row.roi_net_pct)}</span>}
      </td>
      <td>
        <FundingCell row={row} horizonH={horizonH} now={now} />
      </td>
      <td
        className="strong"
        title={row.funding_known ? `Итог = профит + фандинг за ${horizonH} ч.` : "Итог = профит; фандинг не получен и не учтён."}
      >
        <span className={signClass(row.total_pct)}>{signedPct(row.total_pct)}</span>
      </td>
      <td>
        <OpenButton token={token} row={row} onResult={onResult} />
      </td>
    </tr>
  );
}

function GapTable({
  groups,
  expanded,
  toggle,
  scope,
  token,
  horizonH,
  sizeUsd,
  now,
  onResult,
}: {
  groups: Group[];
  expanded: Set<string>;
  toggle: (key: string) => void;
  scope: string;
  token: string;
  horizonH: string;
  sizeUsd: string;
  now: number;
  onResult: (message: string | null) => void;
}) {
  const common = { token, horizonH, sizeUsd, now, onResult };
  return (
    <table className="grid feed-table">
      <thead>
        <tr>
          <th className="left">МОНЕТА</th>
          <th className="left">ЛОНГ</th>
          <th className="left">ШОРТ</th>
          <th title={`прибыль на $${sizeUsd} после стакана и комиссий, если цены сойдутся`}>ПРОФИТ ${sizeUsd}</th>
          <th title={`что пара получит (+) или заплатит (−) по фандингу за ${horizonH} ч, и время до расчёта: лонг/шорт`}>
            ФАНДИНГ
          </th>
          <th title="профит + фандинг">ИТОГ</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {groups.map((group) => {
          const key = `${scope}:${group.token}`;
          const open = expanded.has(key);
          return (
            <Fragment key={key}>
              <GapLine row={group.head} others={group.others.length} expanded={open} nested={false} onToggle={() => toggle(key)} {...common} />
              {open &&
                group.others.map((row) => (
                  <GapLine key={row.key} row={row} others={0} expanded={false} nested onToggle={() => undefined} {...common} />
                ))}
            </Fragment>
          );
        })}
      </tbody>
    </table>
  );
}

export function FeedScreen({ token, snapshot }: { token: string; snapshot: Snapshot | null }) {
  const [filters, setFilters] = useState<Filters>(loadFilters);
  const [tradeMessage, setTradeMessage] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  // Numbers follow the socket; only the order of the rows is held while the cursor is over the table,
  // so a line cannot slide out from under a click. Holding the numbers instead would age the prices,
  // and a gap priced on stale prices is exactly the fake one this terminal must not show.
  const [holding, setHolding] = useState(false);
  const heldOrder = useRef<Record<string, string[]>>({});

  const feed: FeedView | undefined = snapshot?.feed;
  const now = Date.now();

  useEffect(() => {
    try {
      localStorage.setItem("ludik.feed.filters", JSON.stringify(filters));
    } catch {
      // storage may be unavailable
    }
  }, [filters]);

  const set = <K extends keyof Filters>(key: K, value: Filters[K]) => setFilters((current) => ({ ...current, [key]: value }));
  const toggle = (key: string) =>
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  const settings = feed?.settings;
  const minRoi = Number(settings?.min_roi_pct ?? 0);
  const horizonH = settings?.funding_horizon_h ?? "8";
  const sizeUsd = settings?.size_usd ?? "—";
  const enterAfterMs = settings?.enter_after_ms ?? 0;

  const { feedGroups, radarGroups } = useMemo(() => {
    const number = (value: string, fallback: number) => (value.trim() === "" ? fallback : Number(value));
    const roiMax = number(filters.roiMax, Infinity);
    const volumeMin = number(filters.volumeMin, 0);
    const capacityMin = number(filters.capacityMin, 0);
    const query = filters.search.trim().toUpperCase();
    const keep = (row: FeedRow) => {
      // A blocked pair cannot be opened and its profit cannot be trusted — stale book, too thin, suspicious,
      // blacklisted, below the exchange minimums. Such a row is not shown at all.
      // "no_book" is not a fault: that is every radar row until its spread comes close enough to the threshold
      // for the terminal to subscribe to the books. Dropping it would empty the radar.
      if (row.block !== null && row.block !== "no_book") return false;
      // The minimum is the terminal's feed threshold; the maximum filters out fake gaps of different assets.
      if (Number(row.roi_net_pct ?? -Infinity) > roiMax) return false;
      if (row.volume24h_weak_usd !== null && Number(row.volume24h_weak_usd) < volumeMin) return false;
      if (capacityMin && Number(row.capacity_usd ?? 0) < capacityMin) return false;
      if (!filters.showSuspicious && row.suspicious) return false;
      if (filters.longExchange && row.long?.exchange !== filters.longExchange) return false;
      if (filters.shortExchange && row.short?.exchange !== filters.shortExchange) return false;
      if (!filters.showReadOnly && (READ_ONLY.has(row.long?.exchange ?? "") || READ_ONLY.has(row.short?.exchange ?? ""))) return false;
      if (query && !row.token.includes(query)) return false;
      return true;
    };
    const feedRows = (feed?.rows ?? []).filter(keep);
    const radarRows = (feed?.radar ?? []).filter(keep);
    const feedTokens = new Set(feedRows.map((row) => row.token));
    // A coin in the feed lists its other pairs that are above the threshold right now; its head is always a feed gap.
    const feedKeys = new Set(feedRows.map((row) => row.key));
    const aboveThreshold = radarRows.filter((row) => feedTokens.has(row.token) && row.roi_net_pct !== null && Number(row.roi_net_pct) >= minRoi);
    return {
      feedGroups: groupByToken([...feedRows, ...aboveThreshold], filters.sort, (row) => feedKeys.has(row.key)),
      radarGroups: groupByToken(radarRows.filter((row) => !feedTokens.has(row.token)), filters.sort),
    };
  }, [feed, filters, minRoi]);

  // Remember the order the screen is showing, but only while the cursor is away from it.
  useEffect(() => {
    if (holding) return;
    heldOrder.current = {
      feed: feedGroups.map((group) => group.token),
      radar: radarGroups.map((group) => group.token),
    };
  });

  const inHeldOrder = (groups: Group[], scope: string): Group[] => {
    if (!holding) return groups;
    const rank = new Map((heldOrder.current[scope] ?? []).map((tokenName, index) => [tokenName, index]));
    // A coin that appeared while the cursor is here goes to the end rather than pushing the others around.
    return [...groups].sort((a, b) => (rank.get(a.token) ?? Infinity) - (rank.get(b.token) ?? Infinity));
  };

  const exchanges = (snapshot?.exchanges ?? []).map((exchange) => exchange.name);
  const common = { expanded, toggle, token, horizonH, sizeUsd, now, onResult: setTradeMessage };

  return (
    <div className="gaps">
      <section className="feed" onMouseEnter={() => setHolding(true)} onMouseLeave={() => setHolding(false)}>
        <div className="toolbar filters feed-filters">
          <label title="строки с профитом выше — почти всегда разные монеты под одним тикером">
            профит ≤ <input value={filters.roiMax} onChange={(event) => set("roiMax", event.target.value)} />%
          </label>
          <label>
            объём 24ч ≥ $ <input value={filters.volumeMin} onChange={(event) => set("volumeMin", event.target.value)} />
          </label>
          <label title="сколько долларов на ногу выдерживают стаканы, пока профит выше порога">
            глубина ≥ $ <input value={filters.capacityMin} onChange={(event) => set("capacityMin", event.target.value)} />
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
          <SettingsForm
            token={token}
            sizeUsd={settings?.size_usd}
            minRoiPct={settings?.min_roi_pct}
            enterAfterMs={settings?.enter_after_ms}
            horizonH={settings?.funding_horizon_h}
          />
          <span className="sort-buttons">
            сорт:
            {(["total", "profit"] as SortKey[]).map((value) => (
              <button key={value} className={filters.sort === value ? "tab active" : "tab"} onClick={() => set("sort", value)}>
                {value === "total" ? "итог" : "профит"}
              </button>
            ))}
          </span>
        </div>

        {feedGroups.length > 0 && <GapTable groups={inHeldOrder(feedGroups, "feed")} scope="feed" {...common} />}
        {feedGroups.length === 0 && (
          <p className="empty muted">
            {feed ? `Сейчас нет вилок выше порога дольше ${Math.round(enterAfterMs / 1000)} с.` : "Лента запускается…"}
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

        {radarGroups.length > 0 && (
          <>
            <div className="toolbar section-title">
              <span>РАДАР · лучшие спреды сейчас, ниже порога ленты</span>
            </div>
            <GapTable groups={inHeldOrder(radarGroups, "radar")} scope="radar" {...common} />
          </>
        )}
      </section>
      <PortfolioColumn token={token} snapshot={snapshot} />
    </div>
  );
}
