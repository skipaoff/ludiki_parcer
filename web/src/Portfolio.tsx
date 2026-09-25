// VFP: The open-pairs column — a card per pair with live prices, funding and profit if closed now by the books, foreign positions and the accounts behind them.
// Changes when: what an open pair shows, or the read-only actions on it, change (PLAN.md, sections 3.1 and 10).
// Anti-goal:
// 1. Order buttons active while trading is disabled — they stay disabled with the reason.
// 2. Colour without meaning — green and red mark money made and lost; a red border marks a lost leg or a near liquidation.

import { useEffect, useState } from "react";
import { price, rateText, signClass, signedPct, signedUsd, until } from "./money";
import { compact } from "./Pairs";
import { apiSend } from "./session";
import { confirmed, explainFailure, fastTrading, tradingAction } from "./trading";
import type { PortfolioView, PositionView, Snapshot, TradeCard } from "./types";

const ISSUE_TEXT: Record<string, string> = {
  long_missing: "лонга нет на бирже",
  short_missing: "шорта нет на бирже",
  long_qty_mismatch: "количество лонга не совпадает",
  short_qty_mismatch: "количество шорта не совпадает",
};

function pct(value: string | null | undefined, digits = 2): string {
  return value == null ? "—" : `${Number(value).toFixed(digits)}%`;
}

function inTrade(openedMs: number, nowMs: number): string {
  const seconds = Math.max(0, Math.floor((nowMs - openedMs) / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = String(Math.floor((seconds % 3600) / 60)).padStart(2, "0");
  return `${hours}:${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

export function PortfolioColumn({ token, snapshot }: { token: string; snapshot: Snapshot | null }) {
  const portfolio: PortfolioView | undefined = snapshot?.portfolio;
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const now = Date.now();
  const keyed = (snapshot?.exchanges ?? []).filter((exchange) => exchange.keys !== "none");
  const trading = snapshot?.trading?.enabled ?? false;
  const fast = fastTrading(snapshot?.trading);
  const openPairs = snapshot?.pairs.open ?? 0;

  useEffect(() => {
    if (openPairs === 0) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [openPairs]);

  const act = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setMessage(null);
    try {
      await action();
    } catch (reason) {
      setMessage(explainFailure(reason));
    } finally {
      setBusy(false);
    }
  };

  return (
    <aside className="pairs">
      <div className="toolbar">
        <span>
          ОТКРЫТЫЕ ПАРЫ {snapshot?.pairs.open ?? 0}
          <span className="muted" title="риск-лимит одновременно открытых пар: [trading] max_open_pairs в config.toml">
            {" "}
            · лимит {snapshot?.pairs.limit ?? 0}
          </span>
          {snapshot?.pairs.sleep_blocked ? <span className="muted"> · сон Windows запрещён</span> : null}
        </span>
        <button
          className="action"
          disabled={busy || !trading || !(portfolio?.trades.some((trade) => trade.status === "open") ?? false)}
          title={trading ? "закрыть все открытые пары рыночными ордерами" : "торговля выключена"}
          onClick={() => {
            if (window.confirm("Закрыть все открытые пары рыночными ордерами?")) {
              void act(async () => {
                const result = await tradingAction<{ closed: string[]; failed: Record<string, string> }>("/api/trading/close-all", token);
                const failed = Object.values(result.failed);
                if (failed.length) throw new Error(failed.map((text) => explainFailure(new Error(text))).join("; "));
              });
            }
          }}
        >
          [ЗАКРЫТЬ ВСЁ]
        </button>
      </div>
      {message && <p className="empty level-warning">{message}</p>}

      {keyed.length === 0 && (
        <p className="empty muted">Чтобы видеть свои позиции, добавьте ключи в Настройки → Биржи.</p>
      )}

      {portfolio?.trades.map((trade) => (
        <PairCard
          key={trade.id}
          trade={trade}
          now={now}
          busy={busy}
          trading={trading}
          onClose={() => {
            const question = `Закрыть пару ${trade.token}: ${trade.qty_tokens} ${trade.token}, лонг ${trade.long.exchange.toUpperCase()}, шорт ${trade.short.exchange.toUpperCase()}.\nОбе ноги закроются по рынку.`;
            if (!confirmed(fast, question)) return;
            act(() => tradingAction("/api/trading/close", token, { trade_id: trade.id }));
          }}
          onCloseLeg={(side) => act(() => tradingAction("/api/trading/close-leg", token, { trade_id: trade.id, side }))}
          onCloseRecord={() => act(() => apiSend("POST", `/api/portfolio/trades/${trade.id}/close-record`, token))}
        />
      ))}
      {keyed.length > 0 && portfolio && portfolio.trades.length === 0 && <p className="empty muted">Открытых пар нет.</p>}

      {portfolio && portfolio.suggestions.length > 0 && (
        <section className="card-section">
          <div className="section-caption">МОЖНО ВЗЯТЬ ПАРОЙ</div>
          {portfolio.suggestions.map((suggestion) => (
            <div className="pair-card" key={`${suggestion.long.symbol}|${suggestion.short.symbol}`}>
              <div className="row-line">
                <span className="strong">{suggestion.token}</span>
                <span>
                  L {suggestion.long.exchange.toUpperCase()} {price(suggestion.long.entry)} · S {suggestion.short.exchange.toUpperCase()}{" "}
                  {price(suggestion.short.entry)}
                </span>
              </div>
              <div className="row-line muted">
                <span>
                  {suggestion.long.qty_tokens} / {suggestion.short.qty_tokens} {suggestion.token}
                </span>
                <button
                  className="action"
                  disabled={busy}
                  onClick={() =>
                    act(() =>
                      apiSend("POST", "/api/portfolio/pairs", token, {
                        long: { exchange: suggestion.long.exchange, symbol: suggestion.long.symbol },
                        short: { exchange: suggestion.short.exchange, symbol: suggestion.short.symbol },
                      }),
                    )
                  }
                >
                  [ВЗЯТЬ ПАРУ]
                </button>
              </div>
            </div>
          ))}
        </section>
      )}

      {portfolio && portfolio.foreign.length > 0 && (
        <section className="card-section">
          <div className="section-caption">ЧУЖИЕ ПОЗИЦИИ · терминал ими не управляет</div>
          {portfolio.foreign.map((position) => (
            <ForeignRow key={`${position.exchange}|${position.symbol}|${position.side}`} position={position} />
          ))}
        </section>
      )}

      {portfolio && Object.keys(portfolio.accounts).length > 0 && (
        <section className="card-section muted">
          {Object.entries(portfolio.accounts).map(([name, account]) => (
            <div className="row-line" key={name}>
              <span>{name.toUpperCase()}</span>
              <span className={account.error ? "level-warning" : ""}>
                {account.error
                  ? `позиции не получены: ${account.error}`
                  : `$${compact(account.equity_usd)} · свободно $${compact(account.available_usd)} · позиций ${account.positions}${
                      account.polled_ms ? ` · ${Math.round((now - account.polled_ms) / 1000)}с назад` : ""
                    }`}
              </span>
            </div>
          ))}
        </section>
      )}
    </aside>
  );
}

function PairCard({
  trade,
  now,
  busy,
  trading,
  onClose,
  onCloseLeg,
  onCloseRecord,
}: {
  trade: TradeCard;
  now: number;
  busy: boolean;
  trading: boolean;
  onClose: () => void;
  onCloseLeg: (side: "long" | "short") => void;
  onCloseRecord: () => void;
}) {
  const lost = trade.status === "leg_lost";
  const liqNear = trade.liq_worst_pct != null && Number(trade.liq_worst_pct) < 10;
  const stale = trade.book_age_ms == null || trade.book_age_ms > 5000;
  const transitional = trade.status === "opening" ? "ОТКРЫВАЕТСЯ" : trade.status === "closing" ? "закрывается" : null;
  const status =
    transitional ??
    (lost ? "НОГА ПОТЕРЯНА" : trade.issues.length ? trade.issues.map((issue) => ISSUE_TEXT[issue] ?? issue).join(", ") : stale ? "нет данных" : "открыта");
  const nextFunding =
    trade.funding_next_ms != null && trade.funding_next_usd != null
      ? ` · след. ${signedUsd(trade.funding_next_usd)} через ${until(trade.funding_next_ms, now)}`
      : "";
  return (
    <div className={lost ? "pair-card alarm" : "pair-card"}>
      <div className="row-line">
        <span className="strong">{trade.token}</span>
        <span
          className={`strong ${signClass(trade.pnl_now_usd)}`}
          title="профит, если закрыть обе ноги сейчас по стаканам: разница цен входа и выхода, комиссии и полученный фандинг"
        >
          {signedUsd(trade.pnl_now_usd)} {trade.pnl_now_pct != null && signedPct(trade.pnl_now_pct)}
        </span>
      </div>
      <div className="row-line">
        <span>
          ЛОНГ {trade.long.exchange.toUpperCase()} {price(trade.long.entry)} → <span className="strong">{price(trade.long_now)}</span>
        </span>
        <span className="muted" title="ставка фандинга биржи за расчёт и интервал">
          {rateText(trade.funding_long)}
        </span>
      </div>
      <div className="row-line">
        <span>
          ШОРТ {trade.short.exchange.toUpperCase()} {price(trade.short.entry)} → <span className="strong">{price(trade.short_now)}</span>
        </span>
        <span className="muted" title="ставка фандинга биржи за расчёт и интервал">
          {rateText(trade.funding_short)}
        </span>
      </div>
      <div className="row-line">
        <span title="фандинг, уже полученный (+) или заплаченный (−) по этой паре, и ближайший расчёт">
          фандинг <span className={signClass(trade.funding_usd)}>{signedUsd(trade.funding_usd)}</span>
          <span className={Number(trade.funding_next_usd ?? 0) < 0 ? "loss" : "muted"}>{nextFunding}</span>
        </span>
        <span className={liqNear ? "alarm-text strong" : "muted"}>до ликв. {pct(trade.liq_worst_pct, 1)}</span>
      </div>
      <div className="row-line muted">
        <span>
          {trade.qty_tokens} {trade.token} · {inTrade(trade.opened_at_ms, now)} в сделке · {status}
        </span>
        {!lost && (
          <button
            className="action"
            disabled={busy || !trading || trade.status !== "open"}
            title={trading ? "закрыть обе ноги рыночными reduce-only ордерами" : "торговля выключена"}
            onClick={onClose}
          >
            [ЗАКРЫТЬ]
          </button>
        )}
      </div>
      {lost && (
        <div className="row-line">
          <span>
            <button
              className="action"
              disabled={busy || !trading}
              onClick={() => {
                if (window.confirm(`Закрыть лонг ${trade.token} на ${trade.long.exchange.toUpperCase()} рыночным ордером?`)) onCloseLeg("long");
              }}
            >
              [ЗАКРЫТЬ ЛОНГ]
            </button>{" "}
            <button
              className="action"
              disabled={busy || !trading}
              onClick={() => {
                if (window.confirm(`Закрыть шорт ${trade.token} на ${trade.short.exchange.toUpperCase()} рыночным ордером?`)) onCloseLeg("short");
              }}
            >
              [ЗАКРЫТЬ ШОРТ]
            </button>
          </span>
          <button
            className="action"
            disabled={busy}
            onClick={() => {
              if (window.confirm(`Закрыть запись о паре ${trade.token}? Ордера не отправляются, позиции на биржах не трогаются.`)) onCloseRecord();
            }}
          >
            [ЗАКРЫТЬ ЗАПИСЬ]
          </button>
        </div>
      )}
    </div>
  );
}

function ForeignRow({ position }: { position: PositionView }) {
  return (
    <div className="row-line">
      <span>
        {position.exchange.toUpperCase()} {position.symbol} {position.side === "long" ? "L" : "S"}
        {position.leverage ? ` x${position.leverage}` : ""}
      </span>
      <span>
        {position.qty_tokens} · вход {price(position.entry)} · ликв. {price(position.liquidation)}
      </span>
    </div>
  );
}
