// VFP: The Pairs service screen — every Binance–MEXC pair with units, steps, suspicion, links for manual checks and the manual flags.
// Changes when: pairs gain attributes or the manual verification workflow changes.
// Anti-goal:
// 1. Hiding why a pair is not tradable — every non-tradable row states its reason.
// 2. Numbers rounded into lies — steps and minimums are shown exactly, only volumes are compacted.

import { useCallback, useEffect, useMemo, useState } from "react";
import { clock } from "./format";
import { apiGet, apiSend } from "./session";
import type { InstrumentsSummary, PairLeg, PairView } from "./types";

type Filter = "all" | "suspicious" | "multiplier" | "blacklisted";
type SortKey = "volume" | "token" | "index";

const REASON_TEXT: Record<string, string> = {
  quote_missing: "нет котировок",
  price_mismatch: "цена за токен расходится — множитель или другая монета",
  index_missing: "нет индексной цены",
  index_mismatch: "индексы расходятся",
};

export function compact(value: string | null): string {
  if (value === null) return "—";
  const number = Number(value);
  const abs = Math.abs(number);
  if (abs >= 1e9) return `${(number / 1e9).toFixed(abs >= 1e10 ? 0 : 1)}B`;
  if (abs >= 1e6) return `${(number / 1e6).toFixed(abs >= 1e7 ? 0 : 1)}M`;
  if (abs >= 1e3) return `${(number / 1e3).toFixed(abs >= 1e4 ? 0 : 1)}K`;
  return String(number);
}

function pct(value: string | null, digits = 2): string {
  return value === null ? "—" : `${Number(value).toFixed(digits)}%`;
}

function price(value: string | null): string {
  if (value === null) return "—";
  const number = Number(value);
  if (number >= 1) return number.toLocaleString("en-US", { maximumFractionDigits: 4 });
  // Five significant digits without exponent notation: 0.00000010315, not 1.0315e-7.
  const decimals = Math.min(15, 4 - Math.floor(Math.log10(number)));
  return number.toFixed(decimals);
}

function multiplier(value: string): string {
  const number = Number(value);
  return number >= 1e6 && number % 1e6 === 0 ? `${number / 1e6}M` : String(number);
}

function hasMultiplier(leg: PairLeg): boolean {
  return Number(leg.price_unit_tokens) !== 1;
}

export function PairsScreen({ token, summary }: { token: string; summary: InstrumentsSummary | undefined }) {
  const [pairs, setPairs] = useState<PairView[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<SortKey>("volume");
  const [busy, setBusy] = useState<string | null>(null);

  const reload = useCallback(() => {
    apiGet<{ pairs: PairView[] }>("/api/pairs", token)
      .then((data) => {
        setPairs(data.pairs);
        setError(null);
      })
      .catch((reason: Error) => setError(reason.message));
  }, [token]);

  useEffect(reload, [reload, summary?.refreshed_at_ms]);

  const visible = useMemo(() => {
    const query = search.trim().toUpperCase();
    const rows = (pairs ?? []).filter((pair) => {
      if (query && !pair.token.includes(query) && !pair.a.symbol.includes(query) && !pair.b.symbol.includes(query)) return false;
      if (filter === "suspicious") return pair.reason !== null;
      if (filter === "multiplier") return hasMultiplier(pair.a) || hasMultiplier(pair.b);
      if (filter === "blacklisted") return pair.blacklisted;
      return true;
    });
    const volume = (pair: PairView) => Number(pair.volume24h_weak_usd ?? -1);
    const index = (pair: PairView) => Number(pair.index_gap_pct ?? 1e9);
    rows.sort((x, y) =>
      sort === "token" ? x.token.localeCompare(y.token) : sort === "index" ? index(y) - index(x) : volume(y) - volume(x),
    );
    return rows;
  }, [pairs, filter, search, sort]);

  const update = async (pair: PairView, flags: { manually_verified?: boolean; blacklisted?: boolean }) => {
    setBusy(pair.key);
    try {
      const updated = await apiSend<PairView>("POST", "/api/pairs/flags", token, { key: pair.key, ...flags });
      setPairs((current) => current?.map((item) => (item.key === updated.key ? updated : item)) ?? null);
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const refresh = async () => {
    setBusy("refresh");
    try {
      await apiSend("POST", "/api/instruments/refresh", token);
      reload();
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="pairs-screen">
      <div className="toolbar">
        <span>
          ПАРЫ {summary?.pairs ?? "…"} · торгуемых {summary?.tradable ?? "…"} · подозрительных {summary?.suspicious ?? "…"} · в
          ч/с {summary?.blacklisted ?? "…"}
          <span className="muted">
            {" "}
            · {summary?.refreshing ? "обновление…" : summary?.refreshed_at_ms ? `обновлено ${clock(summary.refreshed_at_ms)}` : "ещё не загружены"}
          </span>
        </span>
        <button className="action" onClick={() => void refresh()} disabled={busy !== null || summary?.refreshing}>
          [ОБНОВИТЬ]
        </button>
      </div>
      <div className="toolbar filters">
        {(
          [
            ["all", "все"],
            ["suspicious", "подозрительные"],
            ["multiplier", "с множителем"],
            ["blacklisted", "чёрный список"],
          ] as [Filter, string][]
        ).map(([value, label]) => (
          <button key={value} className={filter === value ? "tab active" : "tab"} onClick={() => setFilter(value)}>
            {label}
          </button>
        ))}
        <input placeholder="поиск монеты" value={search} onChange={(event) => setSearch(event.target.value)} spellCheck={false} />
      </div>
      {(error || summary?.error) && <p className="level-warning empty">{error ?? summary?.error}</p>}
      {!pairs ? (
        <p className="empty muted">загрузка…</p>
      ) : (
        <div className="table-scroll">
          <table className="grid pairs-table">
            <thead>
              <tr>
                <th className="left sortable" onClick={() => setSort("token")}>
                  МОНЕТА{sort === "token" ? " ↑" : ""}
                </th>
                <th className="left">BINANCE</th>
                <th className="left">MEXC</th>
                <th>ЦЕНА/ТОКЕН</th>
                <th>ШАГ, ТОК.</th>
                <th>МИН., ТОК.</th>
                <th className="sortable" onClick={() => setSort("index")}>
                  Δ ИНДЕКСА{sort === "index" ? " ↓" : ""}
                </th>
                <th className="sortable" onClick={() => setSort("volume")}>
                  ОБЪЁМ 24Ч{sort === "volume" ? " ↓" : ""}
                </th>
                <th className="left">СТАТУС</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {visible.map((pair) => (
                <tr key={pair.key} className={pair.tradable ? "" : "muted"}>
                  <td className="left strong">{pair.token}</td>
                  <td className="left">
                    <LegLink leg={pair.a} />
                    {hasMultiplier(pair.a) && <span className="muted"> ×{multiplier(pair.a.price_unit_tokens)}</span>}
                  </td>
                  <td className="left">
                    <LegLink leg={pair.b} />
                    <span className="muted"> конт. {compact(pair.b.qty_unit_tokens)}</span>
                  </td>
                  <td>{price(pair.a.price_per_token)}</td>
                  <td>{pair.common_step_tokens}</td>
                  <td>{pair.min_qty_tokens}</td>
                  <td>{pct(pair.index_gap_pct, 3)}</td>
                  <td>${compact(pair.volume24h_weak_usd)}</td>
                  <td className="left">
                    <Status pair={pair} />
                  </td>
                  <td className="left nowrap">
                    <button
                      className="action"
                      disabled={busy !== null}
                      onClick={() => void update(pair, { manually_verified: !pair.manually_verified })}
                    >
                      {pair.manually_verified ? "[снять ✓]" : "[проверено]"}
                    </button>{" "}
                    <button
                      className="action"
                      disabled={busy !== null}
                      onClick={() => void update(pair, { blacklisted: !pair.blacklisted })}
                    >
                      {pair.blacklisted ? "[из ч/с]" : "[в ч/с]"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {visible.length === 0 && <p className="empty muted">ничего не найдено</p>}
        </div>
      )}
    </div>
  );
}

function LegLink({ leg }: { leg: PairLeg }) {
  return leg.url ? (
    <a href={leg.url} target="_blank" rel="noreferrer noopener">
      {leg.symbol}
    </a>
  ) : (
    <span>{leg.symbol}</span>
  );
}

function Status({ pair }: { pair: PairView }) {
  if (pair.blacklisted) return <span className="strong">чёрный список</span>;
  if (pair.reason && pair.manually_verified) return <span>✓ проверено вручную</span>;
  if (pair.reason) return <span className="strong">? {REASON_TEXT[pair.reason] ?? pair.reason}</span>;
  return <span>ок{pair.manually_verified ? " · ✓" : ""}</span>;
}
