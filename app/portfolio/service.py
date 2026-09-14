"""
VFP: Keeps the terminal's picture of open pairs true to the exchanges — polls positions, balances and funding, glues legs into pairs, computes live PnL and liquidation distance, and records it.
Changes when: how open pairs are tracked, restored or shown changes (PLAN.md, section 10).
Anti-goal:
1. Sending orders — this stage is read-only; closing and aligning are stage 6.
2. Declaring a leg lost on one bad poll — a leg must be missing on two polls in a row.
3. Managing positions the terminal did not open or was not told about — those stay foreign until assigned.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Callable

from app.config.settings import PortfolioSettings
from app.core.portfolio import (
    TradeLegs,
    entry_spread_pct,
    estimated_entry_fees_usd,
    pair_metrics,
    pair_quantity,
    reconcile,
    suggest_pairs,
)
from app.core.qty import common_step_tokens
from app.core.roi import exit_quote
from app.core.schemas import Fees, Instrument, LegSide
from app.exchanges.base import Balance, Position
from app.instruments.service import PairRecord
from app.journal.journal import Journal, Level
from app.market.state import MarketState
from app.storage.ids import LocalIds

log = logging.getLogger(__name__)

ACTIVE_STATUSES = ("opening", "open", "closing", "leg_lost")
MISSING_POLLS_FOR_LOST = 2


class PortfolioError(ValueError):
    pass


def _ts(ms: int | None) -> datetime | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, UTC)


def _text(value: Decimal | None, digits: int = 8) -> str | None:
    if value is None:
        return None
    if value == 0:
        return "0"
    return format(round(value, max(0, digits - value.adjusted() - 1)).normalize(), "f")


@dataclass
class Trade:
    id: int
    token: str
    long_exchange: str
    long_symbol: str
    short_exchange: str
    short_symbol: str
    qty_tokens: Decimal
    entry_long_avg: Decimal
    entry_short_avg: Decimal
    opened_at_ms: int
    status: str = "open"
    pair_id: int | None = None
    size_usd: Decimal = Decimal(0)
    leverage_long: int = 0
    leverage_short: int = 0
    margin_mode: str = "isolated"
    fees_usd: Decimal = Decimal(0)
    funding_usd: Decimal = Decimal(0)
    notes: str | None = None
    closed_at_ms: int | None = None
    close_reason: str | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    episode_id: int | None = None
    roi_expected_entry: Decimal | None = None
    roi_actual_entry: Decimal | None = None
    exit_long_avg: Decimal | None = None
    exit_short_avg: Decimal | None = None
    exit_spread_expected: Decimal | None = None
    exit_spread_actual: Decimal | None = None
    pnl_gross_usd: Decimal | None = None
    pnl_net_usd: Decimal | None = None
    pnl_net_pct: Decimal | None = None
    click_to_fill_ms_long: int | None = None
    click_to_fill_ms_short: int | None = None
    missing_polls: int = 0
    issues: tuple[str, ...] = ()
    liq_warned: bool = False
    last_sample_ms: int = 0
    metrics: dict[str, Any] = field(default_factory=dict)

    def legs(self) -> TradeLegs:
        return TradeLegs(self.id, self.token, self.long_exchange, self.long_symbol, self.short_exchange, self.short_symbol, self.qty_tokens)

    def row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "strategy": "price_gap",
            "episode_id": self.episode_id,
            "pair_id": self.pair_id,
            "token": self.token,
            "long_exchange": self.long_exchange,
            "short_exchange": self.short_exchange,
            "qty_tokens": self.qty_tokens,
            "size_usd": self.size_usd,
            "leverage_long": self.leverage_long,
            "leverage_short": self.leverage_short,
            "margin_mode": self.margin_mode,
            "status": self.status,
            "opened_at": _ts(self.opened_at_ms),
            "closed_at": _ts(self.closed_at_ms),
            "entry_long_avg": self.entry_long_avg,
            "entry_short_avg": self.entry_short_avg,
            "roi_expected_entry": self.roi_expected_entry,
            "roi_actual_entry": self.roi_actual_entry
            if self.roi_actual_entry is not None
            else entry_spread_pct(self.entry_long_avg, self.entry_short_avg),
            "exit_spread_expected": self.exit_spread_expected,
            "exit_spread_actual": self.exit_spread_actual,
            "exit_long_avg": self.exit_long_avg,
            "exit_short_avg": self.exit_short_avg,
            "fees_usd": self.fees_usd,
            "funding_usd": self.funding_usd,
            "pnl_gross_usd": self.pnl_gross_usd,
            "pnl_net_usd": self.pnl_net_usd,
            "pnl_net_pct": self.pnl_net_pct,
            "close_reason": self.close_reason,
            "click_to_fill_ms_long": self.click_to_fill_ms_long,
            "click_to_fill_ms_short": self.click_to_fill_ms_short,
            "settings_snapshot": {
                **self.settings,
                "legs": {"long_symbol": self.long_symbol, "short_symbol": self.short_symbol},
            },
            "notes": self.notes,
        }


@dataclass
class _ExchangeState:
    positions: list[Position] = field(default_factory=list)
    balance: Balance | None = None
    balance_ms: float | None = None
    polled_ms: float | None = None
    error: str | None = None


class PortfolioService:
    def __init__(
        self,
        exchanges: Any,
        catalog: Any,
        state: MarketState,
        database: Any,
        submit: Callable[[str, dict[str, Any]], None],
        journal: Journal,
        settings: PortfolioSettings,
        taker_fee_pct: Callable[[str], Decimal],
        on_active_change: Callable[[bool], None] = lambda active: None,
        clock_ms: Callable[[], float] = lambda: time.time() * 1000,
        ids: LocalIds | None = None,
    ) -> None:
        """exchanges is app.exchanges.service.ExchangeService, catalog is app.instruments.service.InstrumentService."""
        self._exchanges = exchanges
        self._catalog = catalog
        self._state = state
        self._database = database
        self._submit = submit
        self._journal = journal
        self._settings = settings
        self._taker_fee_pct = taker_fee_pct
        self._on_active_change = on_active_change
        self._clock_ms = clock_ms
        self._ids = ids or LocalIds()
        self.trades: dict[int, Trade] = {}
        self._accounts: dict[str, _ExchangeState] = {}
        self._foreign: list[Position] = []
        self._active = False
        self._poke: asyncio.Event | None = None

    # ── catalog helpers ──────────────────────────────────────────────────────

    def _instruments(self, exchange: str) -> dict[str, Instrument]:
        result = {}
        for record in self._catalog.records():
            for leg in (record.assessment.a, record.assessment.b):
                if leg.exchange == exchange:
                    result[leg.symbol_raw] = leg
        return result

    def _record_for(self, exchange_a: str, symbol_a: str, exchange_b: str, symbol_b: str) -> PairRecord | None:
        legs = {(exchange_a, symbol_a), (exchange_b, symbol_b)}
        for record in self._catalog.records():
            if {(record.assessment.a.exchange, record.assessment.a.symbol_raw), (record.assessment.b.exchange, record.assessment.b.symbol_raw)} == legs:
                return record
        return None

    def pinned_pair_keys(self) -> list[str]:
        """Pairs whose order books must stay subscribed: every active trade."""
        keys = []
        for trade in self.trades.values():
            record = self._record_for(trade.long_exchange, trade.long_symbol, trade.short_exchange, trade.short_symbol)
            if record is not None:
                keys.append(record.key)
        return keys

    def _fees(self, trade: Trade) -> Fees:
        return Fees(self._taker_fee_pct(trade.long_exchange), self._taker_fee_pct(trade.short_exchange))

    # ── restore ──────────────────────────────────────────────────────────────

    async def load(self) -> int:
        if not self._database.ready:
            return 0
        rows = await self._database.pool.fetch(
            "SELECT * FROM trades WHERE status::text = ANY($1::text[]) ORDER BY opened_at", list(ACTIVE_STATUSES)
        )
        for row in rows:
            snapshot = row["settings_snapshot"] or {}
            legs = snapshot.get("legs") or {}
            if not legs.get("long_symbol") or not legs.get("short_symbol"):
                log.warning("trade %s has no leg symbols recorded, skipped", row["id"])
                continue
            self.trades[row["id"]] = Trade(
                id=row["id"],
                token=row["token"],
                long_exchange=row["long_exchange"],
                long_symbol=legs["long_symbol"],
                short_exchange=row["short_exchange"],
                short_symbol=legs["short_symbol"],
                qty_tokens=row["qty_tokens"],
                entry_long_avg=row["entry_long_avg"],
                entry_short_avg=row["entry_short_avg"],
                opened_at_ms=int(row["opened_at"].timestamp() * 1000),
                status=str(row["status"]),
                pair_id=row["pair_id"],
                size_usd=row["size_usd"],
                leverage_long=row["leverage_long"],
                leverage_short=row["leverage_short"],
                margin_mode=row["margin_mode"],
                fees_usd=row["fees_usd"],
                funding_usd=row["funding_usd"],
                notes=row["notes"],
                settings={key: value for key, value in snapshot.items() if key != "legs"},
                episode_id=row["episode_id"],
                roi_expected_entry=row["roi_expected_entry"],
                roi_actual_entry=row["roi_actual_entry"],
                exit_spread_expected=row["exit_spread_expected"],
                click_to_fill_ms_long=row["click_to_fill_ms_long"],
                click_to_fill_ms_short=row["click_to_fill_ms_short"],
            )
        if self.trades:
            self._journal.emit(Level.INFO, "portfolio", "restored", pairs=len(self.trades))
        self._update_active()
        return len(self.trades)

    # ── polling ──────────────────────────────────────────────────────────────

    def _keyed_exchanges(self) -> list[str]:
        return [item["name"] for item in self._exchanges.snapshot() if item["keys"] in ("saved", "ok", "warning", "checking")]

    def poke(self) -> None:
        """Poll positions now rather than on the next timer, e.g. after a private stream reported a change."""
        if self._poke is not None:
            self._poke.set()

    async def run_positions(self) -> None:
        self._poke = asyncio.Event()
        while True:
            try:
                await self.poll_positions()
            except Exception:
                log.exception("position poll failed")
            self._poke.clear()
            try:
                await asyncio.wait_for(self._poke.wait(), self._settings.positions_poll_s)
                await asyncio.sleep(0.2)  # let a burst of events settle into one poll
            except TimeoutError:
                pass

    async def poll_positions(self, required: tuple[str, ...] = ()) -> bool:
        """Refresh positions; True only when every keyed exchange (and every required one) answered."""
        exchanges = self._keyed_exchanges()
        results = await asyncio.gather(
            *(self._exchanges.adapter(name).fetch_positions(self._instruments(name)) for name in exchanges),
            return_exceptions=True,
        )
        complete = True
        for name, result in zip(exchanges, results):
            account = self._accounts.setdefault(name, _ExchangeState())
            if isinstance(result, BaseException):
                account.error = f"{type(result).__name__}: {result}"[:300]
                complete = False
                continue
            account.positions = result
            account.polled_ms = self._clock_ms()
            account.error = None
        for name in list(self._accounts):
            if name not in exchanges:
                del self._accounts[name]
        self._apply_reconciliation(complete)
        return complete and all(name in exchanges for name in required)

    def record_for_trade(self, trade: Trade) -> PairRecord | None:
        return self._record_for(trade.long_exchange, trade.long_symbol, trade.short_exchange, trade.short_symbol)

    def _apply_reconciliation(self, complete: bool) -> None:
        positions = [position for account in self._accounts.values() for position in account.positions]
        result = reconcile([trade.legs() for trade in self.trades.values() if trade.status != "closed"], positions)
        polled = set(self._accounts)
        self._foreign = result.foreign
        for trade in self.trades.values():
            issues = result.issues.get(trade.id, ())
            # A leg on an exchange without keys, or on a failed poll, is unknown rather than missing.
            if not complete or trade.long_exchange not in polled or trade.short_exchange not in polled:
                issues = tuple(issue for issue in issues if not issue.endswith("_missing"))
            trade.issues = issues
            missing = any(issue.endswith("_missing") for issue in issues)
            trade.missing_polls = trade.missing_polls + 1 if missing else 0
            if trade.missing_polls >= MISSING_POLLS_FOR_LOST and trade.status == "open":
                trade.status = "leg_lost"
                self._submit("trades", trade.row())
                self._journal.emit(
                    Level.CRITICAL, "portfolio", "leg_lost", trade_id=trade.id, token=trade.token, issues=list(issues)
                )
            elif not missing and trade.status == "leg_lost":
                trade.status = "open"
                self._submit("trades", trade.row())
                self._journal.emit(Level.INFO, "portfolio", "leg_found", trade_id=trade.id, token=trade.token)

    async def run_balances(self) -> None:
        while True:
            for name in self._keyed_exchanges():
                try:
                    balance = await self._exchanges.adapter(name).fetch_balance()
                except Exception as exc:
                    log.warning("balance of %s failed: %s", name, exc)
                    continue
                account = self._accounts.setdefault(name, _ExchangeState())
                account.balance, account.balance_ms = balance, self._clock_ms()
                self._submit(
                    "balance_snapshots",
                    {
                        "ts": _ts(int(self._clock_ms())),
                        "exchange": name,
                        "equity_usd": balance.equity_usd,
                        "available_usd": balance.available_usd,
                        "margin_used_usd": balance.margin_used_usd,
                    },
                )
            await asyncio.sleep(self._settings.balances_poll_s)

    async def run_funding(self) -> None:
        while True:
            await asyncio.sleep(self._settings.funding_poll_s)
            for trade in list(self.trades.values()):
                if trade.status == "closed":
                    continue
                try:
                    long = await self._exchanges.adapter(trade.long_exchange).fetch_funding_usd(trade.long_symbol, trade.opened_at_ms)
                    short = await self._exchanges.adapter(trade.short_exchange).fetch_funding_usd(trade.short_symbol, trade.opened_at_ms)
                except Exception as exc:
                    log.warning("funding of trade %s failed: %s", trade.id, exc)
                    continue
                total = long + short
                if total != trade.funding_usd:
                    trade.funding_usd = total
                    self._submit("trades", trade.row())

    # ── live metrics ─────────────────────────────────────────────────────────

    async def run_metrics(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("portfolio tick failed")
            await asyncio.sleep(1)

    def tick(self) -> None:
        now = int(self._clock_ms())
        result = reconcile(
            [trade.legs() for trade in self.trades.values()],
            [position for account in self._accounts.values() for position in account.positions],
        )
        for trade in self.trades.values():
            long_position, short_position = result.legs.get(trade.id, (None, None))
            long_key, short_key = (trade.long_exchange, trade.long_symbol), (trade.short_exchange, trade.short_symbol)
            long_book, short_book = self._state.books.get(long_key), self._state.books.get(short_key)
            exit = exit_quote(long_book, short_book, trade.qty_tokens) if long_book and short_book else None
            mark_long = self._mark(long_key, long_position)
            mark_short = self._mark(short_key, short_position)
            fees = self._fees(trade)
            metrics = pair_metrics(
                trade.entry_long_avg,
                trade.entry_short_avg,
                exit,
                fees,
                trade.fees_usd,
                trade.funding_usd,
                mark_long,
                long_position.liquidation_price if long_position else None,
                mark_short,
                short_position.liquidation_price if short_position else None,
            )
            book_ages = [self._state.book_age_ms(key) for key in (long_key, short_key)]
            trade.metrics = {
                "entry_spread_pct": _text(metrics.entry_spread_pct, 4),
                "exit_spread_pct": _text(metrics.exit_spread_pct, 4),
                "pnl_now_usd": _text(metrics.pnl_now_usd, 6),
                "liq_long_pct": _text(metrics.liq_distance_long_pct, 4),
                "liq_short_pct": _text(metrics.liq_distance_short_pct, 4),
                "liq_worst_pct": _text(metrics.worst_liquidation_pct, 4),
                "mark_long": _text(mark_long),
                "mark_short": _text(mark_short),
                "book_age_ms": None if None in book_ages else int(max(book_ages)),
            }
            worst = metrics.worst_liquidation_pct
            if worst is not None and worst < self._settings.liquidation_warning_pct and not trade.liq_warned:
                trade.liq_warned = True
                self._journal.emit(Level.CRITICAL, "portfolio", "liquidation_near", trade_id=trade.id, token=trade.token, distance_pct=str(round(worst, 2)))
            elif worst is not None and worst >= self._settings.liquidation_warning_pct * Decimal("1.2"):
                trade.liq_warned = False
            if trade.status != "closed" and now - trade.last_sample_ms >= 1000:
                trade.last_sample_ms = now
                self._submit(
                    "position_samples",
                    {
                        "ts": _ts(now),
                        "trade_id": trade.id,
                        "mark_long": mark_long,
                        "mark_short": mark_short,
                        "bid_long_exit_vwap": exit.long_exit_avg if exit else None,
                        "ask_short_exit_vwap": exit.short_exit_avg if exit else None,
                        "exit_spread_pct": metrics.exit_spread_pct,
                        "pnl_now_usd": metrics.pnl_now_usd,
                        "liq_dist_long_pct": metrics.liq_distance_long_pct,
                        "liq_dist_short_pct": metrics.liq_distance_short_pct,
                    },
                )
        self._update_active()

    def _mark(self, key: tuple[str, str], position: Position | None) -> Decimal | None:
        mark = self._state.marks.get(key)
        if mark is not None and mark.mark:
            return Decimal(str(mark.mark))
        return position.mark_price if position else None

    def _update_active(self) -> None:
        active = any(trade.status != "closed" for trade in self.trades.values())
        if active != self._active:
            self._active = active
            self._on_active_change(active)

    # ── hooks for execution ──────────────────────────────────────────────────

    def add_trade(self, trade: Trade) -> None:
        self.trades[trade.id] = trade
        self._submit("trades", trade.row())
        self._update_active()

    def save(self, trade: Trade) -> None:
        self._submit("trades", trade.row())

    def finish(self, trade: Trade) -> None:
        """A closed or failed pair leaves the active set; its final row is written."""
        self._submit("trades", trade.row())
        self.trades.pop(trade.id, None)
        self._update_active()

    def legs_of(self, trade: Trade) -> tuple[Position | None, Position | None]:
        positions = [position for account in self._accounts.values() for position in account.positions]
        return reconcile([trade.legs()], positions).legs[trade.id]

    def available_usd(self, exchange: str, max_age_ms: float) -> Decimal | None:
        account = self._accounts.get(exchange)
        if account is None or account.balance is None or account.balance_ms is None:
            return None
        if self._clock_ms() - account.balance_ms > max_age_ms:
            return None
        return account.balance.available_usd

    def open_tokens(self) -> frozenset[str]:
        return frozenset(trade.token for trade in self.trades.values() if trade.status != "closed")

    def open_notional_usd(self) -> Decimal:
        return sum((trade.qty_tokens * trade.entry_long_avg for trade in self.trades.values() if trade.status != "closed"), Decimal(0))

    # ── user actions ─────────────────────────────────────────────────────────

    async def assign_pair(self, long_exchange: str, long_symbol: str, short_exchange: str, short_symbol: str) -> dict[str, Any]:
        positions = {(p.exchange, p.symbol_raw, p.side): p for p in self._foreign}
        long = positions.get((long_exchange, long_symbol, LegSide.LONG))
        short = positions.get((short_exchange, short_symbol, LegSide.SHORT))
        if long is None or short is None:
            raise PortfolioError("both legs must be foreign positions: a long and a short")
        if long.exchange == short.exchange or long.token != short.token:
            raise PortfolioError("a pair needs the same token on two different exchanges")
        record = self._record_for(long_exchange, long_symbol, short_exchange, short_symbol)
        if record is None:
            raise PortfolioError("these contracts are not a pair in the catalog")
        step = common_step_tokens(record.assessment.a, record.assessment.b)
        qty = pair_quantity(long, short, step)
        if qty <= 0:
            raise PortfolioError("the legs share no quantity on the common step")
        trade = Trade(
            id=self._ids.next(),
            token=long.token,
            long_exchange=long.exchange,
            long_symbol=long.symbol_raw,
            short_exchange=short.exchange,
            short_symbol=short.symbol_raw,
            qty_tokens=qty,
            entry_long_avg=long.entry_price,
            entry_short_avg=short.entry_price,
            opened_at_ms=int(self._clock_ms()),
            pair_id=record.pair_id,
            size_usd=qty * long.entry_price,
            leverage_long=long.leverage or 0,
            leverage_short=short.leverage or 0,
            margin_mode=long.margin_mode or short.margin_mode or "isolated",
            notes="assigned by hand from existing positions",
            settings={"source": "manual_assign"},
        )
        trade.fees_usd = estimated_entry_fees_usd(qty, trade.entry_long_avg, trade.entry_short_avg, self._fees(trade))
        self.trades[trade.id] = trade
        self._submit("trades", trade.row())
        self._journal.emit(Level.INFO, "portfolio", "pair_assigned", trade_id=trade.id, token=trade.token, qty_tokens=str(qty))
        self._apply_reconciliation(True)
        self.tick()
        return self.describe_trade(trade)

    async def close_record(self, trade_id: int) -> dict[str, Any]:
        """Mark a pair closed outside the terminal (by hand on the exchanges, or liquidated); no orders are sent."""
        trade = self.trades.get(trade_id)
        if trade is None:
            raise PortfolioError("unknown trade")
        trade.status = "closed"
        trade.closed_at_ms = int(self._clock_ms())
        trade.close_reason = "external"
        self._submit("trades", trade.row())
        self._journal.emit(Level.INFO, "portfolio", "record_closed", trade_id=trade.id, token=trade.token)
        del self.trades[trade_id]
        self._update_active()
        return {"id": str(trade_id), "status": "closed"}

    # ── views ────────────────────────────────────────────────────────────────

    def describe_trade(self, trade: Trade) -> dict[str, Any]:
        return {
            "id": str(trade.id),
            "token": trade.token,
            "status": trade.status,
            "issues": list(trade.issues),
            "long": {"exchange": trade.long_exchange, "symbol": trade.long_symbol, "entry": _text(trade.entry_long_avg)},
            "short": {"exchange": trade.short_exchange, "symbol": trade.short_symbol, "entry": _text(trade.entry_short_avg)},
            "qty_tokens": _text(trade.qty_tokens, 12),
            "opened_at_ms": trade.opened_at_ms,
            "fees_usd": _text(trade.fees_usd, 6),
            "funding_usd": _text(trade.funding_usd, 6),
            **trade.metrics,
        }

    @staticmethod
    def _describe_position(position: Position) -> dict[str, Any]:
        return {
            "exchange": position.exchange,
            "symbol": position.symbol_raw,
            "token": position.token,
            "side": position.side.value,
            "qty_tokens": _text(position.qty_tokens, 12),
            "entry": _text(position.entry_price),
            "liquidation": _text(position.liquidation_price),
            "leverage": position.leverage,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "trades": [self.describe_trade(trade) for trade in self.trades.values()],
            "foreign": [self._describe_position(position) for position in self._foreign],
            "suggestions": [
                {"token": long.token, "long": self._describe_position(long), "short": self._describe_position(short)}
                for long, short in suggest_pairs(self._foreign)
            ],
            "accounts": {
                name: {
                    "polled_ms": account.polled_ms,
                    "error": account.error,
                    "positions": len(account.positions),
                    "equity_usd": _text(account.balance.equity_usd, 8) if account.balance else None,
                    "available_usd": _text(account.balance.available_usd, 8) if account.balance else None,
                }
                for name, account in self._accounts.items()
            },
        }

    @property
    def open_count(self) -> int:
        return sum(1 for trade in self.trades.values() if trade.status != "closed")
