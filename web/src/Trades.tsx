// VFP: The Trades screen — one row per finished pair with expected against actual entry and exit, fees, funding, PnL, filters and CSV export.
// Changes when: the trade journal gains a column or a filter (PLAN.md, section 3.2).
// Anti-goal:
// 1. Totals computed in the browser — the terminal's statistics endpoint gives them.

import { useCallback, useEffect, useState } from "react";
import { compact } from "./Pairs";
import { apiGet } from "./session";

const TRADING_EXCHANGES = ["binance", "mexc", "gate", "aster", "bingx"];

interface TradeRow {
  id: string;
  token: string;
  long_exchange: string;
  short_exchange: string;
  qty_tokens: string;
  size_usd: string;
  opened_at: number;
  closed_at: number;
  status: string;
  roi_expected_entry: string | null;
  roi_actual_entry: string | null;
  exit_spread_expected: string | null;
  exit_spread_actual: string | null;
  fees_usd: string | null;
  funding_usd: string | null;
  pnl_net_usd: string | null;
  pnl_net_pct: string | null;
  close_reason: string | null;
  notes: string | null;
}

interface Totals {
  trades: number;
  wins: number;
  failed: number;
  win_rate: number | null;
  pnl_net_usd: string;
  fees_usd: string;
  funding_usd: string;
  avg_duration_s: string | null;
}

const PERIODS: [string, number | null][] = [
  ["сегодня", 1],
  ["7 дней", 7],
  ["30 дней", 30],
  ["всё", null],
];

const CLOSE_REASON: Record<string, string> = {
  manual: "вручную",
  close_all: "закрыть всё",
  auto: "авто",
  leg_failure: "сбой ноги",
  liquidation: "ликвидация",
  external: "вне терминала",
};

export function pct(value: string | null, digits = 2): string {
  return value === null ? "—" : `${Number(value).toFixed(digits)}%`;
}

export function usd(value: string | null): string {
  if (value === null) return "—";
  const number = Number(value);
  return `${number > 0 ? "+" : number < 0 ? "−" : ""}$${Math.abs(number).toFixed(2)}`;
}

export function duration(seconds: number | null): string {
  if (seconds === null || Number.isNaN(seconds)) return "—";
  const total = Math.round(seconds);
  if (total < 60) return `${total}с`;
  if (total < 3600) return `${Math.floor(total / 60)}м ${total % 60}с`;
  return `${Math.floor(total / 3600)}ч ${Math.floor((total % 3600) / 60)}м`;
}

function startOf(days: number | null): number | null {
  if (days === null) return null;
  const date = new Date();
  if (days === 1) {
    date.setHours(0, 0, 0, 0);
    return date.getTime();
  }
  return Date.now() - days * 86_400_000;
}

export function TradesScreen({ token }: { token: string }) {
  const [period, setPeriod] = useState<number | null>(7);
  const [coin, setCoin] = useState("");
  const [longExchange, setLongExchange] = useState("");
  const [shortExchange, setShortExchange] = useState("");
  const [rows, setRows] = useState<TradeRow[] | null>(null);
  const [totals, setTotals] = useState<Totals | null>(null);
  const [error, setError] = useState<string | null>(null);

  const query = useCallback(() => {
    const params = new URLSearchParams();
    const from = startOf(period);
    if (from !== null) params.set("from_ms", String(from));
    if (coin.trim()) params.set("token", coin.trim());
    if (longExchange) params.set("long", longExchange);
    if (shortExchange) params.set("short", shortExchange);
    return params;
  }, [period, coin, longExchange, shortExchange]);

  useEffect(() => {
    const params = query();
    const statsParams = new URLSearchParams();
    if (params.get("from_ms")) statsParams.set("from_ms", params.get("from_ms")!);
    Promise.all([
      apiGet<{ trades: TradeRow[] }>(`/api/trades?${params}`, token),
      apiGet<{ totals: Totals }>(`/api/trades/stats?${statsParams}`, token),
    ])
      .then(([list, stats]) => {
        setRows(list.trades);
        setTotals(stats.totals);
        setError(null);
      })
      .catch((reason: Error) => setError(reason.message));
  }, [query, token]);

  const exportCsv = async () => {
    try {
      const response = await fetch(`/api/trades.csv?${query()}`, { headers: { Authorization: `Bearer ${token}` } });
      if (!response.ok) throw new Error(String(response.status));
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = `ludik-trades-${new Date().toISOString().slice(0, 10)}.csv`;
      link.click();
      URL.revokeObjectURL(url);
    } catch (reason) {
      setError((reason as Error).message);
    }
  };

  return (
    <div className="pairs-screen">
      <div className="toolbar filters">
        {PERIODS.map(([label, days]) => (
          <button key={label} className={period === days ? "tab active" : "tab"} onClick={() => setPeriod(days)}>
            {label}
          </button>
        ))}
        <select value={longExchange} onChange={(event) => setLongExchange(event.target.value)}>
          <option value="">лонг: все</option>
          {TRADING_EXCHANGES.map((name) => (
            <option key={name} value={name}>
              лонг {name.toUpperCase()}
            </option>
          ))}
        </select>
        <select value={shortExchange} onChange={(event) => setShortExchange(event.target.value)}>
          <option value="">шорт: все</option>
          {TRADING_EXCHANGES.map((name) => (
            <option key={name} value={name}>
              шорт {name.toUpperCase()}
            </option>
          ))}
        </select>
        <input placeholder="монета" value={coin} onChange={(event) => setCoin(event.target.value)} spellCheck={false} />
        <button className="action" onClick={() => void exportCsv()}>
          [CSV]
        </button>
      </div>
      {totals && (
        <div className="toolbar muted">
          <span>
            за период: сделок {totals.trades} · прибыльных {totals.wins} ({totals.win_rate === null ? "—" : `${Math.round(totals.win_rate * 100)}%`}) ·
            сбоев ноги {totals.failed} · PnL <span className="strong">{usd(totals.pnl_net_usd)}</span> · комиссии ${Number(totals.fees_usd).toFixed(2)} ·
            фандинг {usd(totals.funding_usd)} · средняя сделка {duration(totals.avg_duration_s === null ? null : Number(totals.avg_duration_s))}
          </span>
        </div>
      )}
      {error && <p className="empty level-warning">{error}</p>}
      {rows && rows.length === 0 && <p className="empty muted">Закрытых сделок за период нет.</p>}
      {rows && rows.length > 0 && (
        <div className="table-scroll">
          <table className="grid feed-table">
            <thead>
              <tr>
                <th className="left">ЗАКРЫТА</th>
                <th className="left">МОНЕТА</th>
                <th className="left">ЛОНГ → ШОРТ</th>
                <th>КОЛ-ВО</th>
                <th title="ожидаемый при клике / фактический по исполнениям, после комиссий">ROI ОЖИД./ФАКТ</th>
                <th>ВЫХОД ОЖИД./ФАКТ</th>
                <th>КОМИССИИ</th>
                <th>ФАНДИНГ</th>
                <th>PnL</th>
                <th>PnL %</th>
                <th>В СДЕЛКЕ</th>
                <th className="left">ПРИЧИНА</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id} className={row.status === "leg_failed" ? "muted" : ""} title={row.notes ?? undefined}>
                  <td className="left">{new Date(row.closed_at).toLocaleString("ru-RU")}</td>
                  <td className="left strong">{row.token}</td>
                  <td className="left">
                    {row.long_exchange.toUpperCase()} → {row.short_exchange.toUpperCase()}
                  </td>
                  <td>{compact(row.qty_tokens)}</td>
                  <td>
                    {pct(row.roi_expected_entry)} / {pct(row.roi_actual_entry)}
                  </td>
                  <td>
                    {pct(row.exit_spread_expected)} / {pct(row.exit_spread_actual)}
                  </td>
                  <td>${row.fees_usd === null ? "—" : Number(row.fees_usd).toFixed(2)}</td>
                  <td>{usd(row.funding_usd)}</td>
                  <td className="strong">{usd(row.pnl_net_usd)}</td>
                  <td>{pct(row.pnl_net_pct)}</td>
                  <td>{duration((row.closed_at - row.opened_at) / 1000)}</td>
                  <td className="left">
                    {row.status === "leg_failed" ? "сбой ноги при открытии" : CLOSE_REASON[row.close_reason ?? ""] ?? row.close_reason ?? "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
