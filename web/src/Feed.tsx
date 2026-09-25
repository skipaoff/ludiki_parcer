// VFP: The Gaps screen — one line per coin with its most interesting pair (long, short, profit on the size after book and fees, funding, expected result, interest), the coin's other pairs on click, the radar below the feed and the open-pairs column.
// Changes when: feed columns, grouping, row states, filters or the radar presentation change (PLAN.md, section 3.1).
// Anti-goal:
// 1. Recomputing trading numbers in the browser — the terminal computes; the screen filters, groups and sorts.
// 2. Numbers that flicker without meaning — book ages and per-second timers stay off the table.
// 3. Hiding why a line cannot be opened — every status has its explanation on hover.

import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { clock } from "./format";
import { rateText, settlementTimers, signClass, signedPct, signedUsd } from "./money";
import { PortfolioColumn } from "./Portfolio";
import { apiSend } from "./session";
import { confirmed, explainFailure, fastTrading, reasonText, tradingAction } from "./trading";
import type { FeedRow, FeedView, Snapshot } from "./types";

type SortKey = "total" | "profit";

// The feed is read with the eye and the cursor, not watched: at the socket's five frames a second every row
// twitches and nothing can be followed. A gap has to hold 30 s to enter the feed anyway.
const REFRESH_MS = 10_000;

interface Filters {
  roiMin: string;
  roiMax: string;
  volumeMin: string;
  capacityMin: string;
  showSuspicious: boolean;
  showReadOnly: boolean;
  /** Exchanges to watch. Empty means every one of them; a row passes when both its legs are here. */
  exchanges: string[];
  search: string;
  sort: SortKey;
}

const DEFAULT_FILTERS: Filters = {
  roiMin: "",
  roiMax: "15",
  // Measured over 840 recorded gaps (23.09.2026): daily volume barely predicts whether a size fits. 68 % of pairs
  // under 50k could carry $1000 against 71 % of pairs over 1M. Of the 26 gaps that would actually have paid, the
  // second best — DELTA on gate/bingx, +2.07 % — traded 29,692 a day and a 50k floor would have hidden it.
  // This threshold only removes contracts that barely trade; depth is what decides whether the size fits.
  volumeMin: "20000",
  capacityMin: "",
  showSuspicious: false,
  // Off by default: of the 71 recorded gaps above 2 %, 43 had a Variational leg and not one of them could be
  // opened. They crowded the top of the screen with numbers nobody can take.
  showReadOnly: false,
  exchanges: [],
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
    // The two long/short dropdowns became one list of exchanges to watch; anything stored from them is dropped.
    if (!Array.isArray(stored.exchanges)) stored.exchanges = [];
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

/** null: the field is untouched and shows what the terminal holds. A string, empty included, is the typed text. */
type Draft = string | null;

function DraftInput({
  value,
  current,
  onChange,
}: {
  value: Draft;
  current: string | undefined;
  onChange: (next: Draft) => void;
}) {
  return (
    <input
      value={value ?? current ?? ""}
      inputMode="decimal"
      onChange={(event) => onChange(event.target.value)}
      onFocus={(event) => event.target.select()}
    />
  );
}

// Eleven exchanges laid out as checkboxes took a whole line of the screen away from the feed and were read every
// time the eye passed them. Folded into one button, the filter says what is watched in three characters and
// opens the list only when it is being changed.
function ExchangePicker({ all, chosen, onChange }: { all: string[]; chosen: string[]; onChange: (next: string[]) => void }) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const outside = (event: MouseEvent) => {
      if (!box.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", outside);
    return () => document.removeEventListener("mousedown", outside);
  }, [open]);

  const label =
    chosen.length === 0
      ? "ВСЕ"
      : chosen.length <= 2
        ? chosen.map((name) => name.toUpperCase()).join(" + ")
        : `${chosen.length} ИЗ ${all.length}`;

  return (
    <div className="dropdown" ref={box} onKeyDown={(event) => event.key === "Escape" && setOpen(false)}>
      <button className={chosen.length ? "tab active" : "tab"} type="button" onClick={() => setOpen(!open)}>
        {label} ▾
      </button>
      {open && (
        <div className="dropdown-menu">
          <button
            className="dropdown-item"
            type="button"
            onClick={() => {
              onChange([]);
              setOpen(false);
            }}
          >
            все биржи
          </button>
          {all.map((name) => {
            const on = chosen.includes(name);
            return (
              <label key={name} className="dropdown-item">
                <input
                  type="checkbox"
                  checked={on}
                  onChange={() => onChange(on ? chosen.filter((item) => item !== name) : [...chosen, name])}
                />{" "}
                {name.toUpperCase()}
              </label>
            );
          })}
        </div>
      )}
    </div>
  );
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
  // null means "not edited", so the terminal's own value shows through. An empty string is a real edit:
  // with "" standing for both, clearing a field snapped it straight back and the last digit could not be deleted.
  const [size, setSize] = useState<Draft>(null);
  const [threshold, setThreshold] = useState<Draft>(null);
  const [lifetime, setLifetime] = useState<Draft>(null);
  const [horizon, setHorizon] = useState<Draft>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const lifetimeSeconds = enterAfterMs === undefined ? "" : String(enterAfterMs / 1000);

  const changed = (draft: Draft, current: string | undefined) => draft !== null && draft.trim() !== "" && draft !== current;
  const dirty = changed(size, sizeUsd) || changed(threshold, minRoiPct) || changed(lifetime, lifetimeSeconds) || changed(horizon, horizonH);

  const forget = () => {
    setSize(null);
    setThreshold(null);
    setLifetime(null);
    setHorizon(null);
    setMessage(null);
  };

  const apply = async () => {
    const body: Record<string, string> = {};
    if (changed(size, sizeUsd)) body.size_usd = (size as string).trim();
    if (changed(threshold, minRoiPct)) body.min_roi_pct = (threshold as string).trim();
    if (changed(horizon, horizonH)) body.funding_horizon_h = (horizon as string).trim().replace(",", ".");
    if (changed(lifetime, lifetimeSeconds)) {
      const seconds = Number((lifetime as string).trim().replace(",", "."));
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
      forget();
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
      onKeyDown={(event) => {
        if (event.key === "Escape") forget();
      }}
    >
      Размер $<DraftInput value={size} current={sizeUsd} onChange={setSize} /> на ногу · порог ленты{" "}
      <DraftInput value={threshold} current={minRoiPct} onChange={setThreshold} />% · живёт ≥{" "}
      <DraftInput value={lifetime} current={lifetimeSeconds} onChange={setLifetime} />с · фандинг за{" "}
      <DraftInput value={horizon} current={horizonH} onChange={setHorizon} />ч{" "}
      {dirty && (
        <>
          <button className="action" type="submit" disabled={busy}>
            [ПРИМЕНИТЬ]
          </button>{" "}
          <button className="action" type="button" onClick={forget} title="вернуть значения терминала (Esc)">
            [ОТМЕНА]
          </button>
        </>
      )}
      {message && <span className="level-warning"> {message}</span>}
    </form>
  );
}

function OpenButton({
  token,
  row,
  fast,
  onResult,
}: {
  token: string;
  row: FeedRow;
  fast: boolean;
  onResult: (message: string | null) => void;
}) {
  const [sending, setSending] = useState(false);
  const blocks = row.open_blocks ?? ["trading_disabled"];
  const blocked = blocks.length > 0 || sending;
  const question =
    `Открыть ${row.token}: лонг ${row.long?.exchange.toUpperCase() ?? "?"}, шорт ${row.short?.exchange.toUpperCase() ?? "?"}` +
    `\nРазмер $${row.size_usd ?? "?"} на ногу, итог ${row.total_pct ?? "?"}%.\nОрдера уйдут по рынку.`;
  const open = async () => {
    if (!confirmed(fast, question)) return;
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
  const name = leg.exchange.toUpperCase();
  const watchOnly = READ_ONLY.has(leg.exchange);
  return (
    <>
      {leg.url ? (
        <a className="chip" href={leg.url} target="_blank" rel="noreferrer" title={`открыть ${name} в браузере`}>
          {name}
        </a>
      ) : (
        <span className="chip">{name}</span>
      )}
      {watchOnly && (
        <span className="chip chip-note muted" title="торгового API у биржи нет: такую вилку можно только смотреть">
          набл.
        </span>
      )}
    </>
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
  fast,
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
  fast: boolean;
  now: number;
  onResult: (message: string | null) => void;
}) {
  const dim = row.total_pct === null;
  return (
    <tr className={dim ? "muted" : ""}>
      <td className="left strong">
        {nested ? (
          // The branch glyph that used to stand here reads as a letter L in a monospace font; the ticker,
          // greyed against the bold head above it, says the same thing and cannot be misread.
          <span className="muted" title={`ещё одна пара по монете ${row.token}`}>
            {row.token}
          </span>
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
        {row.capped_by !== null && row.size_usd !== null && (
          // The venue will not take the whole size in one market order, so the row is quoted on what it will take.
          <span className="chip chip-note" title={`${row.capped_by.toUpperCase()} не принимает рыночный ордер больше этой суммы, поэтому строка посчитана на неё, а не на $${sizeUsd}`}>
            ${Math.round(Number(row.size_usd))}
          </span>
        )}
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
        <OpenButton token={token} row={row} fast={fast} onResult={onResult} />
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
  fast,
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
  fast: boolean;
  now: number;
  onResult: (message: string | null) => void;
}) {
  const common = { token, horizonH, sizeUsd, fast, now, onResult };
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
  // The table takes one snapshot every REFRESH_MS: at the socket's rate the numbers twitch on every row and
  // the screen cannot be read. The price of that is age, so the section says out loud when its numbers were taken.
  // The order is held besides while the cursor is over the table, so a line cannot slide out from under a click.
  const [holding, setHolding] = useState(false);
  const heldOrder = useRef<Record<string, string[]>>({});
  const [view, setView] = useState<{ feed: FeedView; at: number } | null>(null);
  const takenAt = useRef(0);

  useEffect(() => {
    const incoming = snapshot?.feed;
    if (!incoming) return;
    const at = Date.now();
    if (view === null || at - takenAt.current >= REFRESH_MS) {
      takenAt.current = at;
      setView({ feed: incoming, at });
    }
  }, [snapshot, view]);

  const feed: FeedView | undefined = view?.feed;
  const now = view?.at ?? Date.now();

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

  const { feedGroups, radarGroups, manualGroups, hiddenRows } = useMemo(() => {
    const number = (value: string, fallback: number) => (value.trim() === "" ? fallback : Number(value));
    const roiMin = number(filters.roiMin, -Infinity);
    const roiMax = number(filters.roiMax, Infinity);
    const watched = new Set(filters.exchanges);
    const volumeMin = number(filters.volumeMin, 0);
    // An empty depth field means the size per leg in the feed, as the plan has it: the point of the filter is
    // "will my size fit", and every one of the 26 gaps that would have paid carried at least $1256. The radar is
    // the other half of that promise — it watches spreads whose books are not subscribed to yet, so their depth
    // is unknown and the default floor would empty it. There the filter works only when a number is typed in.
    const typedCapacity = filters.capacityMin.trim() !== "";
    const capacityMin = typedCapacity ? Number(filters.capacityMin) : Number(sizeUsd) || 0;
    const radarCapacityMin = typedCapacity ? capacityMin : 0;
    const query = filters.search.trim().toUpperCase();
    // Hidden rows are counted by reason: a screen that silently drops half the market cannot be trusted,
    // and the reason is usually the size — a book that carries $100 a leg may not carry $2000.
    const hidden = new Map<string, number>();
    const drop = (reason: string) => {
      hidden.set(reason, (hidden.get(reason) ?? 0) + 1);
      return false;
    };
    const keep = (row: FeedRow, depthFloor: number) => {
      // A blocked pair cannot be opened and its profit cannot be trusted — stale book, too thin, suspicious,
      // blacklisted, below the exchange minimums. Such a row is not shown at all.
      // "no_book" is not a fault: that is every radar row until its spread comes close enough to the threshold
      // for the terminal to subscribe to the books. Dropping it would empty the radar.
      if (row.block !== null && row.block !== "no_book") return drop(row.block.split(":")[0]);
      // The minimum is the terminal's feed threshold; the maximum filters out fake gaps of different assets.
      if (Number(row.roi_net_pct ?? -Infinity) > roiMax) return drop("профит выше максимума");
      if (roiMin > -Infinity && Number(row.roi_net_pct ?? -Infinity) < roiMin) return drop("профит ниже минимума");
      if (row.volume24h_weak_usd !== null && Number(row.volume24h_weak_usd) < volumeMin) return drop("мал объём 24ч");
      if (depthFloor && Number(row.capacity_usd ?? 0) < depthFloor) return drop("мала глубина");
      if (!filters.showSuspicious && row.suspicious) return drop("подозрительные");
      // Both legs must be on watched exchanges: a pair is only useful when you would trade on either side of it.
      if (watched.size > 0 && !(watched.has(row.long?.exchange ?? "") && watched.has(row.short?.exchange ?? ""))) {
        return drop("биржа не выбрана");
      }
      if (!filters.showReadOnly && (READ_ONLY.has(row.long?.exchange ?? "") || READ_ONLY.has(row.short?.exchange ?? ""))) {
        return drop("без торговли");
      }
      if (query && !row.token.includes(query)) return false;
      return true;
    };
    // Pairs the API refuses orders on live in their own section: they are worth seeing and cannot be clicked,
    // so mixing them into the feed would put unopenable rows above openable ones.
    const byHand = (row: FeedRow) => row.manual_only;
    // Every row passes the filter exactly once, so the hidden count is a count of rows and not of passes.
    const keptFeed = (feed?.rows ?? []).filter((row) => keep(row, capacityMin));
    const keptRadar = (feed?.radar ?? []).filter((row) => keep(row, radarCapacityMin));
    const feedRows = keptFeed.filter((row) => !byHand(row));
    const radarRows = keptRadar.filter((row) => !byHand(row));
    const manualRows = [...keptFeed, ...keptRadar].filter(byHand);
    const feedTokens = new Set(feedRows.map((row) => row.token));
    // A coin in the feed lists its other pairs that are above the threshold right now; its head is always a feed gap.
    const feedKeys = new Set(feedRows.map((row) => row.key));
    const aboveThreshold = radarRows.filter((row) => feedTokens.has(row.token) && row.roi_net_pct !== null && Number(row.roi_net_pct) >= minRoi);
    return {
      feedGroups: groupByToken([...feedRows, ...aboveThreshold], filters.sort, (row) => feedKeys.has(row.key)),
      radarGroups: groupByToken(radarRows.filter((row) => !feedTokens.has(row.token)), filters.sort),
      manualGroups: groupByToken(manualRows, filters.sort),
      hiddenRows: [...hidden.entries()].sort((a, b) => b[1] - a[1]),
    };
  }, [feed, filters, minRoi, sizeUsd]);

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
  // Sorting is a view choice, not a filter: resetting the filters leaves it alone.
  const touched = (Object.keys(DEFAULT_FILTERS) as (keyof Filters)[]).some(
    (key) => key !== "sort" && filters[key] !== DEFAULT_FILTERS[key],
  );
  const fast = fastTrading(snapshot?.trading);
  const common = { expanded, toggle, token, horizonH, sizeUsd, fast, now, onResult: setTradeMessage };

  return (
    <div className="gaps">
      <section className="feed" onMouseEnter={() => setHolding(true)} onMouseLeave={() => setHolding(false)}>
        <div className="filter-panel">
          <div className="filter-row">
            <label title="профит по стакану на твой размер, после комиссий. Ниже минимума строки не показываются">
              профит{" "}
              <input
                className="narrow"
                placeholder="мин"
                value={filters.roiMin}
                inputMode="decimal"
                onFocus={(event) => event.target.select()}
                onKeyDown={(event) => event.key === "Escape" && set("roiMin", "")}
                onChange={(event) => set("roiMin", event.target.value)}
              />
              <span className="muted">…</span>
              <input
                className="narrow"
                placeholder="макс"
                value={filters.roiMax}
                inputMode="decimal"
                onFocus={(event) => event.target.select()}
                onKeyDown={(event) => event.key === "Escape" && set("roiMax", "")}
                onChange={(event) => set("roiMax", event.target.value)}
              />
              %
            </label>
            <label title="оборот за сутки по слабой ноге. Отсекает мёртвые контракты, но глубина говорит о размере точнее">
              объём 24ч ≥ ${" "}
              <input
                value={filters.volumeMin}
                inputMode="decimal"
                onFocus={(event) => event.target.select()}
                onKeyDown={(event) => event.key === "Escape" && set("volumeMin", "")}
                onChange={(event) => set("volumeMin", event.target.value)}
              />
            </label>
            <label title="сколько долларов на ногу выдерживают стаканы, пока профит выше порога. Прямая мера того, возьмётся ли твой размер — в отличие от объёма за сутки. Ставь свой размер на ногу, а лучше полтора: ёмкость считается по входу, выход бывает тоньше">
              глубина ≥ ${" "}
              <input
                value={filters.capacityMin}
                placeholder={sizeUsd === "—" ? "" : sizeUsd}
                inputMode="decimal"
                onFocus={(event) => event.target.select()}
                onKeyDown={(event) => event.key === "Escape" && set("capacityMin", "")}
                onChange={(event) => set("capacityMin", event.target.value)}
              />
            </label>
            <input
              className="search"
              placeholder="монета"
              value={filters.search}
              onKeyDown={(event) => event.key === "Escape" && set("search", "")}
              onChange={(event) => set("search", event.target.value)}
            />
          </div>

          <div className="filter-row">
            <span className="muted">показывать:</span>
            <label
              className="check-label"
              title="Индексы бирж расходятся больше чем на 1 % — вероятно, под одним тикером разные монеты или сломан множитель. Снимите галочку, чтобы убрать такие вилки из ленты."
            >
              <input type="checkbox" checked={filters.showSuspicious} onChange={(event) => set("showSuspicious", event.target.checked)} />{" "}
              подозрительные
            </label>
            <label
              className="check-label"
              title="Вилки, где хотя бы одна нога на бирже без торгового API. Их видно и считает, но кнопка «Открыть» для них не работает."
            >
              <input type="checkbox" checked={filters.showReadOnly} onChange={(event) => set("showReadOnly", event.target.checked)} />{" "}
              без торговли
            </label>
            <span
              className="muted"
              title="какие биржи смотрим. Пара попадает в ленту, только если обе её ноги на отмеченных биржах. Ничего не отмечено — смотрим все"
            >
              биржи:
            </span>
            <ExchangePicker all={exchanges} chosen={filters.exchanges} onChange={(next) => set("exchanges", next)} />
            {touched && (
              <button className="action" type="button" onClick={() => setFilters({ ...DEFAULT_FILTERS, sort: filters.sort })}>
                [СБРОС ФИЛЬТРОВ]
              </button>
            )}
          </div>

        </div>
        <div className="toolbar muted">
          <SettingsForm
            token={token}
            sizeUsd={settings?.size_usd}
            minRoiPct={settings?.min_roi_pct}
            enterAfterMs={settings?.enter_after_ms}
            horizonH={settings?.funding_horizon_h}
          />
          <span className="taken-at" title={`таблица берёт снимок раз в ${REFRESH_MS / 1000} с, чтобы строки не дёргались. Это время — когда взят показанный снимок`}>
            данные {clock(now)}
          </span>
          <span className="sort-buttons">
            сорт:
            {(["total", "profit"] as SortKey[]).map((value) => (
              <button key={value} className={filters.sort === value ? "tab active" : "tab"} onClick={() => set("sort", value)}>
                {value === "total" ? "итог" : "профит"}
              </button>
            ))}
          </span>
        </div>

        <div className="feed-pane">
          {feedGroups.length > 0 && <GapTable groups={inHeldOrder(feedGroups, "feed")} scope="feed" {...common} />}
          {feedGroups.length === 0 && (
            <p className="empty muted">
              {feed ? `Сейчас нет вилок выше порога дольше ${Math.round(enterAfterMs / 1000)} с.` : "Лента запускается…"}
            </p>
          )}
          {hiddenRows.length > 0 && (
            // The count stays on screen because an empty feed must not look like an empty market; the breakdown
            // moved to the hint, where it is there when a row is missing and out of the way when it is not.
            <p
              className="empty muted"
              title={
                "строки, которые терминал посчитал, но не показал:\n" +
                hiddenRows.map(([reason, count]) => `${count} — ${reasonText(reason)}`).join("\n") +
                "\n\nглубина считается на размер сделки: уменьшите его, и часть строк вернётся"
              }
            >
              скрыто {hiddenRows.reduce((sum, [, count]) => sum + count, 0)}
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
        </div>

        {radarGroups.length > 0 && (
          <div className="feed-pane">
            <div className="toolbar section-title">
              <span>
                <span className="chip">РАДАР</span> <span className="muted">лучшие спреды сейчас, ниже порога ленты</span>
              </span>
            </div>
            <GapTable groups={inHeldOrder(radarGroups, "radar")} scope="radar" {...common} />
          </div>
        )}

        {manualGroups.length > 0 && (
          <div className="feed-pane">
            <div className="toolbar section-title">
              <span>
                <span className="chip">ТОЛЬКО РУКАМИ</span>{" "}
                <span className="muted">биржа не принимает ордера по API — такую вилку открывают на её сайте</span>
              </span>
            </div>
            <GapTable groups={inHeldOrder(manualGroups, "manual")} scope="manual" {...common} />
          </div>
        )}
      </section>
      <PortfolioColumn token={token} snapshot={snapshot} />
    </div>
  );
}
