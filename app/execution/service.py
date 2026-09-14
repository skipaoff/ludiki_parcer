"""
VFP: Turns a click into orders and orders back into a safe state — warm-up, pre-trade checks, parallel market orders, unknown outcomes resolved, failed legs hedged, pairs closed from real positions and settled.
Changes when: how pairs are opened, closed or repaired changes (PLAN.md, section 9).
Anti-goal:
1. A naked leg without an alarm — every path ends hedged, closed, or flagged critical with the pair kept visible.
2. Deciding what core/legs.py decides — this dispatcher executes the actions the core returns.
3. Sending anything while trading is disabled, or closing from quantities the exchanges did not just confirm.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Awaitable, Callable

from app.config.settings import READ_ONLY_EXCHANGES, TradingSettings
from app.core.actions import CloseLeg, MarkLegLost, MarkTradeClosed, MarkTradeFailed, MarkTradeOpen, QueryOrderStatus, RaiseAlert, ReduceLeg
from app.core.legs import CloseResult, LegResult, OrderOutcome, decide_close, decide_open
from app.core.portfolio import entry_spread_pct, estimated_entry_fees_usd, settle, weighted_price
from app.core.pretrade import OpenFacts, RiskLimits, client_order_id, open_blocks
from app.core.schemas import Fees, Instrument, LegSide
from app.exchanges.base import FillReport, OrderReport
from app.instruments.service import PairRecord
from app.journal.journal import Journal, Level
from app.portfolio.service import Trade
from app.storage.ids import LocalIds

log = logging.getLogger(__name__)

NOT_FOUND_CONFIRMATIONS = 2
BACKGROUND_RESOLVE_INTERVAL_S = 5
BACKGROUND_RESOLVE_LIMIT_S = 600
POSITION_POLL_ATTEMPTS = 3


class TradingError(Exception):
    def __init__(self, reasons: list[str]):
        super().__init__(", ".join(reasons))
        self.reasons = reasons


def _ts(ms: float | None) -> datetime | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, UTC)


@dataclass
class OrderTicket:
    id: int
    trade_id: int
    instrument: Instrument
    instrument_id: int | None
    purpose: str
    leg: LegSide
    opening: bool
    qty_tokens: Decimal
    qty_units: Decimal
    client_order_id: str
    clicked_ms: float
    sent_ms: float | None = None
    report: OrderReport | None = None
    not_found: int = 0
    fills: list[FillReport] = field(default_factory=list)

    def row(self) -> dict[str, Any]:
        report = self.report
        return {
            "id": self.id,
            "trade_id": self.trade_id,
            "exchange": self.instrument.exchange,
            "instrument_id": self.instrument_id,
            "client_order_id": self.client_order_id,
            "exchange_order_id": report.exchange_order_id if report else None,
            "purpose": self.purpose,
            "side": self.leg.value,
            "qty_tokens": self.qty_tokens,
            "qty_exchange_units": self.qty_units,
            "order_type": "market",
            "reduce_only": not self.opening,
            "clicked_at": _ts(self.clicked_ms),
            "sent_at": _ts(self.sent_ms),
            "ack_at": _ts(report.ack_ts_ms) if report else None,
            "filled_at": _ts(report.ack_ts_ms) if report and report.outcome in (OrderOutcome.FILLED, OrderOutcome.PARTIAL) else None,
            "status": report.status if report else "sending",
            "filled_qty_tokens": report.filled_tokens if report else Decimal(0),
            "avg_price": report.avg_price if report else None,
            "fee_usd": self.fee_usd(),
            "error_code": report.error_code if report else None,
            "error_message": report.error_message if report else None,
            "request": {
                "symbol": self.instrument.symbol_raw,
                "leg": self.leg.value,
                "opening": self.opening,
                "qty_units": str(self.qty_units),
                "client_order_id": self.client_order_id,
            },
            "response": report.response if report else None,
        }

    def fee_usd(self) -> Decimal | None:
        if self.fills and all(fill.fee_asset in ("USDT", "") for fill in self.fills):
            return sum((fill.fee for fill in self.fills), Decimal(0))
        return self.report.fee_usd if self.report else None

    def avg_price(self) -> Decimal | None:
        if self.fills:
            return weighted_price([(fill.qty_tokens, fill.price) for fill in self.fills])
        return self.report.avg_price if self.report else None

    @property
    def filled_tokens(self) -> Decimal:
        return self.report.filled_tokens if self.report else Decimal(0)


class ExecutionService:
    def __init__(
        self,
        settings: TradingSettings,
        size_usd: Callable[[], Decimal],
        exchanges: Any,
        engine: Any,
        portfolio: Any,
        recorder: Any,
        submit: Callable[[str, dict[str, Any]], None],
        journal: Journal,
        taker_fee_pct: Callable[[str], Decimal],
        balance_max_age_ms: float,
        clock_ms: Callable[[], float] = lambda: time.time() * 1000,
        ids: LocalIds | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        order_events: Any = None,
    ) -> None:
        self._order_events = order_events
        """exchanges: ExchangeService; engine: PriceGapEngine; portfolio: PortfolioService; recorder: EpisodeRecorder."""
        self._settings = settings
        self._size_usd = size_usd
        self._exchanges = exchanges
        self._engine = engine
        self._portfolio = portfolio
        self._recorder = recorder
        self._submit = submit
        self._journal = journal
        self._taker_fee_pct = taker_fee_pct
        self._balance_max_age_ms = balance_max_age_ms
        self._clock_ms = clock_ms
        self._ids = ids or LocalIds()
        self._sleep = sleep
        self._busy: set[str] = set()
        self._warmed: set[tuple[str, str, int, bool]] = set()
        self._warm_failed: dict[tuple[str, str, int, bool], tuple[float, str]] = {}
        self._blocked_until: dict[str, tuple[float, str]] = {}
        self._background: set[asyncio.Task] = set()

    # ── facts ────────────────────────────────────────────────────────────────

    def _leverage(self, exchange: str) -> int:
        return self._settings.leverage(exchange)

    def _keys_accepted(self, exchange: str) -> bool:
        try:
            check = self._exchanges.describe_one(exchange).get("check")
        except KeyError:
            return False
        return bool(check and check.get("accepted"))

    def _exchange_block(self, exchange: str) -> str | None:
        blocked = self._blocked_until.get(exchange)
        if blocked and blocked[0] > self._clock_ms():
            return f"{exchange}:{blocked[1]}"
        return None

    def _warm_key(self, instrument: Instrument) -> tuple[str, str, int, bool]:
        return (instrument.exchange, instrument.symbol_raw, self._leverage(instrument.exchange), self._settings.isolated)

    def _limits(self) -> RiskLimits:
        return RiskLimits(
            max_open_pairs=self._settings.max_open_pairs,
            max_total_usd=self._settings.max_total_usd,
            one_pair_per_token=self._settings.one_pair_per_token,
            margin_buffer_pct=self._settings.margin_buffer_pct,
            entry_min_roi_pct=self._settings.entry_min_roi_pct,
        )

    def open_blocks_for(self, pair_key: str) -> list[str]:
        found = self._engine.current_quote(pair_key)
        if found is None:
            return ["no_quote"]
        record, quote, _ = found
        long, short = quote.long, quote.short
        read_only = [leg.exchange for leg in (long, short) if leg.exchange in READ_ONLY_EXCHANGES]
        if read_only:
            # No other check matters: a pair with a venue that cannot take orders is information, not a trade.
            return [f"exchange_read_only:{name}" for name in read_only]
        fresh = quote.problem != "stale" and self._clock_ms() - quote.ts_ms <= self._engine.quote_max_age_ms
        qty_problem = quote.problem if quote.problem not in (None, "stale") else None
        facts = OpenFacts(
            trading_enabled=self._settings.enabled,
            token=record.assessment.token,
            tradable_pair=record.tradable,
            both_legs_fresh=fresh,
            roi_net_pct=quote.roi_net_pct,
            qty_problem=qty_problem,
            notional_usd=(quote.qty_tokens or Decimal(0)) * (quote.long_avg or Decimal(0)),
            available_long_usd=self._portfolio.available_usd(long.exchange, self._balance_max_age_ms),
            available_short_usd=self._portfolio.available_usd(short.exchange, self._balance_max_age_ms),
            leverage_long=self._leverage(long.exchange),
            leverage_short=self._leverage(short.exchange),
            open_pairs=self._portfolio.open_count,
            open_tokens=self._portfolio.open_tokens(),
            open_notional_usd=self._portfolio.open_notional_usd(),
            keys_accepted=(self._keys_accepted(long.exchange), self._keys_accepted(short.exchange)),
            exchange_blocked=(self._exchange_block(long.exchange), self._exchange_block(short.exchange)),
            warmed_up=self._warm_key(long) in self._warmed and self._warm_key(short) in self._warmed,
            busy=record.assessment.token in self._busy,
        )
        return open_blocks(facts, self._limits())

    # ── warm-up ──────────────────────────────────────────────────────────────

    async def run_warmup(self) -> None:
        while True:
            await self._sleep(2)
            if not self._settings.enabled:
                continue
            for row in self._engine.view().get("rows", []):
                found = self._engine.current_quote(row["key"])
                if found is None:
                    continue
                record = found[0]
                for instrument in (record.assessment.a, record.assessment.b):
                    await self.warm(instrument)

    async def warm(self, instrument: Instrument) -> bool:
        key = self._warm_key(instrument)
        if key in self._warmed:
            return True
        failed = self._warm_failed.get(key)
        if failed and self._clock_ms() - failed[0] < self._settings.warmup_retry_s * 1000:
            return False
        if not self._keys_accepted(instrument.exchange):
            return False
        try:
            await self._exchanges.adapter(instrument.exchange).prepare_symbol(instrument, key[2], key[3])
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"[:300]
            first = key not in self._warm_failed
            self._warm_failed[key] = (self._clock_ms(), reason)
            if first:
                self._journal.emit(Level.WARNING, "trading", "warmup_failed", exchange=instrument.exchange, symbol=instrument.symbol_raw, error=reason)
            return False
        self._warm_failed.pop(key, None)
        self._warmed.add(key)
        return True

    # ── orders ───────────────────────────────────────────────────────────────

    def _ticket(self, trade: Trade, record: PairRecord | None, instrument: Instrument, purpose: str, leg: LegSide, opening: bool, qty_tokens: Decimal, clicked_ms: float, attempt: int = 0) -> OrderTicket:
        instrument_id = None
        if record is not None:
            instrument_id = record.instrument_a_id if instrument.exchange == record.assessment.a.exchange else record.instrument_b_id
        order_id = self._ids.next()
        return OrderTicket(
            id=order_id,
            trade_id=trade.id,
            instrument=instrument,
            instrument_id=instrument_id,
            purpose=purpose,
            leg=leg,
            opening=opening,
            qty_tokens=qty_tokens,
            qty_units=(qty_tokens / instrument.qty_unit_tokens).normalize(),
            client_order_id=client_order_id(order_id, purpose, attempt),
            clicked_ms=clicked_ms,
        )

    async def _send(self, ticket: OrderTicket) -> OrderReport:
        adapter = self._exchanges.adapter(ticket.instrument.exchange)
        ticket.sent_ms = self._clock_ms()
        report = await adapter.place_market_order(
            ticket.instrument,
            ticket.leg,
            ticket.opening,
            ticket.qty_units,
            ticket.client_order_id,
            self._leverage(ticket.instrument.exchange),
            self._settings.isolated,
        )
        ticket.report = report
        if report.error_message and "maintenance" in report.error_message.lower():
            self._blocked_until[ticket.instrument.exchange] = (self._clock_ms() + self._settings.maintenance_block_s * 1000, "maintenance")
        self._submit("orders", ticket.row())
        return report

    async def _resolve(self, ticket: OrderTicket, attempts: int | None = None) -> OrderReport:
        """Query an order with an unknown outcome until the exchange tells what happened, or attempts run out."""
        adapter = self._exchanges.adapter(ticket.instrument.exchange)
        remaining = self._settings.status_query_attempts if attempts is None else attempts
        while ticket.report is not None and ticket.report.outcome is OrderOutcome.UNKNOWN and remaining > 0:
            remaining -= 1
            interval = self._settings.status_query_interval_ms / 1000
            if self._order_events is not None:
                # A private stream event for this order ends the wait early; the status query still decides.
                await self._order_events.wait(ticket.client_order_id, interval)
            else:
                await self._sleep(interval)
            status = await adapter.fetch_order(ticket.instrument, ticket.client_order_id, ticket.qty_tokens)
            if status.status == "not_found":
                # Right after a timeout the order may still be travelling; believe "never existed" only when repeated.
                ticket.not_found += 1
                if ticket.not_found < NOT_FOUND_CONFIRMATIONS:
                    continue
            else:
                ticket.not_found = 0
            if status.outcome is OrderOutcome.UNKNOWN and status.status in ("error", "empty"):
                continue
            ticket.report = replace(status, sent_ts_ms=ticket.report.sent_ts_ms, ack_ts_ms=status.ack_ts_ms or ticket.report.ack_ts_ms)
        self._submit("orders", ticket.row())
        return ticket.report

    async def _collect_fills(self, trade: Trade, tickets: list[OrderTicket]) -> None:
        for ticket in tickets:
            if ticket.report is None or ticket.filled_tokens <= 0:
                continue
            try:
                ticket.fills = await self._exchanges.adapter(ticket.instrument.exchange).fetch_fills(ticket.instrument, ticket.report)
            except Exception as exc:
                log.warning("fills of order %s failed: %s", ticket.client_order_id, exc)
                continue
            self._submit("orders", ticket.row())
            for fill in ticket.fills:
                self._submit(
                    "fills",
                    {
                        "order_id": ticket.id,
                        "exchange_fill_id": fill.exchange_fill_id,
                        "ts": _ts(fill.ts_ms),
                        "price": fill.price,
                        "qty_tokens": fill.qty_tokens,
                        "fee": fill.fee,
                        "fee_asset": fill.fee_asset,
                        "is_maker": fill.is_maker,
                    },
                )

    def _fees_of(self, tickets: list[OrderTicket]) -> Decimal:
        """Real fees where the exchange reported them in USDT, taker-rate estimates for the rest."""
        total = Decimal(0)
        for ticket in tickets:
            if ticket.filled_tokens <= 0:
                continue
            fee = ticket.fee_usd()
            if fee is None:
                price = ticket.avg_price() or Decimal(0)
                fee = ticket.filled_tokens * price * self._taker_fee_pct(ticket.instrument.exchange) / 100
            total += fee
        return total

    def _spawn(self, coroutine: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ── open ─────────────────────────────────────────────────────────────────

    async def open_pair(self, pair_key: str) -> dict[str, Any]:
        reasons = self.open_blocks_for(pair_key)
        if reasons:
            raise TradingError(reasons)
        record, quote, episode_key = self._engine.current_quote(pair_key)
        token = record.assessment.token
        self._busy.add(token)
        try:
            return await self._open(record, quote, episode_key)
        finally:
            self._busy.discard(token)

    async def _open(self, record: PairRecord, quote: Any, episode_key: str | None) -> dict[str, Any]:
        clicked = self._clock_ms()
        long, short = quote.long, quote.short
        qty = quote.qty_tokens
        trade = Trade(
            id=self._ids.next(),
            token=record.assessment.token,
            long_exchange=long.exchange,
            long_symbol=long.symbol_raw,
            short_exchange=short.exchange,
            short_symbol=short.symbol_raw,
            qty_tokens=qty,
            entry_long_avg=quote.long_avg,
            entry_short_avg=quote.short_avg,
            opened_at_ms=int(clicked),
            status="opening",
            pair_id=record.pair_id,
            size_usd=self._size_usd(),
            leverage_long=self._leverage(long.exchange),
            leverage_short=self._leverage(short.exchange),
            margin_mode="isolated" if self._settings.isolated else "cross",
            settings=self._snapshot(),
            episode_id=self._recorder.episode_id(episode_key) if episode_key else None,
            roi_expected_entry=quote.roi_net_pct,
            exit_spread_expected=quote.exit_spread_pct,
        )
        self._portfolio.add_trade(trade)
        if episode_key:
            state = self._engine.episode_state(episode_key)
            if state is not None:
                self._recorder.mark_opened(episode_key, record, state, trade.id, int(clicked))
        self._journal.emit(Level.INFO, "trading", "opening", trade_id=trade.id, token=trade.token, qty_tokens=str(qty), roi_expected=str(quote.roi_net_pct))

        tickets = {
            LegSide.LONG: self._ticket(trade, record, long, "open_long", LegSide.LONG, True, qty, clicked),
            LegSide.SHORT: self._ticket(trade, record, short, "open_short", LegSide.SHORT, True, qty, clicked),
        }
        await asyncio.gather(*(self._send(ticket) for ticket in tickets.values()))
        await asyncio.gather(*(self._resolve(ticket) for ticket in tickets.values()))
        await self._decide_open(trade, record, tickets, clicked)
        return self._portfolio.describe_trade(trade)

    async def _decide_open(self, trade: Trade, record: PairRecord, tickets: dict[LegSide, OrderTicket], clicked: float) -> None:
        long, short = tickets[LegSide.LONG], tickets[LegSide.SHORT]
        actions = decide_open(
            LegResult(LegSide.LONG, long.report.outcome, long.filled_tokens),
            LegResult(LegSide.SHORT, short.report.outcome, short.filled_tokens),
            trade.qty_tokens,
            record.assessment.common_step_tokens,
            record.assessment.min_qty_tokens,
        )
        instruments = {LegSide.LONG: long.instrument, LegSide.SHORT: short.instrument}
        fixes: list[OrderTicket] = []
        for action in actions:
            if isinstance(action, QueryOrderStatus):
                continue  # statuses were already queried; if still unknown, the alert below hands over to the background
            if isinstance(action, RaiseAlert):
                self._journal.emit(action.level if isinstance(action.level, Level) else Level(action.level.value), "trading", action.code, trade_id=trade.id, token=trade.token)
                if action.code == "leg_status_unknown":
                    trade.status = "opening"
                    self._portfolio.save(trade)
                    self._spawn(self._keep_resolving(trade, record, tickets, clicked))
                    return
            elif isinstance(action, (CloseLeg, ReduceLeg)):
                purpose = "fix_leg"
                ticket = self._ticket(trade, record, instruments[action.side], purpose, action.side, False, action.qty_tokens, clicked, attempt=len(fixes) + 1)
                fixes.append(ticket)
                await self._send(ticket)
                report = await self._resolve(ticket)
                if report.outcome is not OrderOutcome.FILLED:
                    trade.status = "leg_lost"
                    self._portfolio.save(trade)
                    self._journal.emit(Level.CRITICAL, "trading", "hedge_fix_failed", trade_id=trade.id, token=trade.token, side=action.side.value, error=report.error_message)
            elif isinstance(action, MarkTradeOpen):
                trade.qty_tokens = action.qty_tokens
                trade.entry_long_avg = long.avg_price() or trade.entry_long_avg
                trade.entry_short_avg = short.avg_price() or trade.entry_short_avg
                fees = Fees(self._taker_fee_pct(trade.long_exchange), self._taker_fee_pct(trade.short_exchange))
                trade.roi_actual_entry = entry_spread_pct(trade.entry_long_avg, trade.entry_short_avg) - fees.round_trip_pct
                trade.fees_usd = estimated_entry_fees_usd(trade.qty_tokens, trade.entry_long_avg, trade.entry_short_avg, fees)
                trade.click_to_fill_ms_long = int((long.report.ack_ts_ms or clicked) - clicked)
                trade.click_to_fill_ms_short = int((short.report.ack_ts_ms or clicked) - clicked)
                if trade.status != "leg_lost":
                    trade.status = "open"
                self._portfolio.save(trade)
                self._journal.emit(
                    Level.INFO, "trading", "opened", trade_id=trade.id, token=trade.token,
                    roi_expected=str(trade.roi_expected_entry), roi_actual=str(round(trade.roi_actual_entry, 4)),
                    ms_long=trade.click_to_fill_ms_long, ms_short=trade.click_to_fill_ms_short,
                )
            elif isinstance(action, MarkTradeFailed):
                if trade.status == "leg_lost":
                    continue
                trade.status = "leg_failed"
                trade.close_reason = "leg_failure"
                trade.closed_at_ms = int(self._clock_ms())
                trade.notes = action.reason
                self._portfolio.finish(trade)
        all_tickets = [long, short, *fixes]
        await self._collect_fills(trade, all_tickets)
        if trade.status == "open":
            entry = [ticket for ticket in (long, short)]
            trade.fees_usd = self._fees_of(entry)
            trade.entry_long_avg = long.avg_price() or trade.entry_long_avg
            trade.entry_short_avg = short.avg_price() or trade.entry_short_avg
            self._portfolio.save(trade)

    async def _keep_resolving(self, trade: Trade, record: PairRecord, tickets: dict[LegSide, OrderTicket], clicked: float) -> None:
        started = self._clock_ms()
        while self._clock_ms() - started < BACKGROUND_RESOLVE_LIMIT_S * 1000:
            await self._sleep(BACKGROUND_RESOLVE_INTERVAL_S)
            for ticket in tickets.values():
                await self._resolve(ticket, attempts=1)
            if all(ticket.report.outcome is not OrderOutcome.UNKNOWN for ticket in tickets.values()):
                self._journal.emit(Level.INFO, "trading", "leg_status_resolved", trade_id=trade.id, token=trade.token)
                await self._decide_open(trade, record, tickets, clicked)
                return
        trade.status = "leg_lost"
        self._portfolio.save(trade)
        self._journal.emit(Level.CRITICAL, "trading", "leg_status_never_resolved", trade_id=trade.id, token=trade.token)

    # ── close ────────────────────────────────────────────────────────────────

    async def close_pair(self, trade_id: int, reason: str = "manual") -> dict[str, Any]:
        trade = self._portfolio.trades.get(trade_id)
        if trade is None:
            raise TradingError(["unknown_trade"])
        if not self._settings.enabled:
            raise TradingError(["trading_disabled"])
        if trade.status in ("opening", "closing") or trade.token in self._busy:
            raise TradingError(["pair_busy"])
        self._busy.add(trade.token)
        try:
            return await self._close(trade, reason)
        finally:
            self._busy.discard(trade.token)

    async def _fresh_legs(self, trade: Trade) -> tuple[Any, Any] | None:
        for _ in range(POSITION_POLL_ATTEMPTS):
            if await self._portfolio.poll_positions(required=(trade.long_exchange, trade.short_exchange)):
                return self._portfolio.legs_of(trade)
            await self._sleep(0.5)
        return None

    async def _close(self, trade: Trade, reason: str) -> dict[str, Any]:
        record = self._portfolio.record_for_trade(trade)
        instruments = self._instruments_of(trade, record)
        previous = trade.status
        trade.status = "closing"
        self._portfolio.save(trade)
        clicked = self._clock_ms()
        self._journal.emit(Level.INFO, "trading", "closing", trade_id=trade.id, token=trade.token, reason=reason)
        closes: list[OrderTicket] = []

        for attempt in range(1, self._settings.close_attempts + 1):
            legs = await self._fresh_legs(trade)
            if legs is None:
                trade.status = previous
                self._portfolio.save(trade)
                self._journal.emit(Level.CRITICAL, "trading", "close_positions_unknown", trade_id=trade.id, token=trade.token)
                raise TradingError(["positions_unknown"])
            outcomes: dict[LegSide, OrderOutcome] = {LegSide.LONG: OrderOutcome.FILLED, LegSide.SHORT: OrderOutcome.FILLED}
            tickets = []
            for side, position in zip((LegSide.LONG, LegSide.SHORT), legs):
                if position is not None and position.qty_tokens > 0:
                    purpose = "close_long" if side is LegSide.LONG else "close_short"
                    tickets.append(self._ticket(trade, record, instruments[side], purpose, side, False, position.qty_tokens, clicked, attempt))
            if tickets:
                await asyncio.gather(*(self._send(ticket) for ticket in tickets))
                await asyncio.gather(*(self._resolve(ticket) for ticket in tickets))
                closes.extend(tickets)
                for ticket in tickets:
                    outcomes[ticket.leg] = ticket.report.outcome
                legs = await self._fresh_legs(trade)
                if legs is None:
                    self._journal.emit(Level.CRITICAL, "trading", "close_positions_unknown", trade_id=trade.id, token=trade.token)
                    trade.status = "leg_lost"
                    self._portfolio.save(trade)
                    raise TradingError(["positions_unknown"])
            remaining = [position.qty_tokens if position is not None else Decimal(0) for position in legs]
            actions = decide_close(
                CloseResult(LegSide.LONG, outcomes[LegSide.LONG], remaining[0]),
                CloseResult(LegSide.SHORT, outcomes[LegSide.SHORT], remaining[1]),
                attempt,
                self._settings.close_attempts,
            )
            if any(isinstance(action, MarkTradeClosed) for action in actions):
                await self._settle(trade, closes, reason)
                return {"id": str(trade.id), "status": trade.status, "pnl_net_usd": str(trade.pnl_net_usd)}
            if any(isinstance(action, MarkLegLost) for action in actions):
                trade.status = "leg_lost"
                self._portfolio.save(trade)
                self._journal.emit(Level.CRITICAL, "trading", "leg_close_failed", trade_id=trade.id, token=trade.token)
                await self._collect_fills(trade, closes)
                raise TradingError(["leg_close_failed"])
            await self._sleep(self._settings.close_retry_pause_ms / 1000)
        raise TradingError(["close_incomplete"])

    def _instruments_of(self, trade: Trade, record: PairRecord | None) -> dict[LegSide, Instrument]:
        if record is None:
            raise TradingError(["pair_not_in_catalog"])
        by_key = {(leg.exchange, leg.symbol_raw): leg for leg in (record.assessment.a, record.assessment.b)}
        return {LegSide.LONG: by_key[(trade.long_exchange, trade.long_symbol)], LegSide.SHORT: by_key[(trade.short_exchange, trade.short_symbol)]}

    async def _settle(self, trade: Trade, closes: list[OrderTicket], reason: str) -> None:
        await self._collect_fills(trade, closes)
        exit_long = weighted_price([(t.filled_tokens, t.avg_price()) for t in closes if t.leg is LegSide.LONG and t.avg_price()])
        exit_short = weighted_price([(t.filled_tokens, t.avg_price()) for t in closes if t.leg is LegSide.SHORT and t.avg_price()])
        try:
            long_funding = await self._exchanges.adapter(trade.long_exchange).fetch_funding_usd(trade.long_symbol, trade.opened_at_ms)
            short_funding = await self._exchanges.adapter(trade.short_exchange).fetch_funding_usd(trade.short_symbol, trade.opened_at_ms)
            trade.funding_usd = long_funding + short_funding
            for exchange, amount in ((trade.long_exchange, long_funding), (trade.short_exchange, short_funding)):
                self._submit("funding_payments", {"trade_id": trade.id, "exchange": exchange, "ts": _ts(self._clock_ms()), "rate": None, "amount_usd": amount})
        except Exception as exc:
            log.warning("funding at close of trade %s failed: %s", trade.id, exc)
        trade.fees_usd = trade.fees_usd + self._fees_of(closes)
        if exit_long is not None and exit_short is not None:
            result = settle(trade.qty_tokens, trade.entry_long_avg, trade.entry_short_avg, exit_long, exit_short, trade.fees_usd, trade.funding_usd)
            trade.exit_long_avg, trade.exit_short_avg = exit_long, exit_short
            trade.exit_spread_actual = result.exit_spread_pct
            trade.pnl_gross_usd, trade.pnl_net_usd, trade.pnl_net_pct = result.pnl_gross_usd, result.pnl_net_usd, result.pnl_net_pct
        trade.status = "closed"
        trade.closed_at_ms = int(self._clock_ms())
        trade.close_reason = reason
        self._portfolio.finish(trade)
        self._journal.emit(Level.INFO, "trading", "closed", trade_id=trade.id, token=trade.token, pnl_net_usd=str(trade.pnl_net_usd), reason=reason)

    async def close_leg(self, trade_id: int, side: LegSide) -> dict[str, Any]:
        """Close one remaining leg of a pair whose other leg is gone; the pair is done when neither leg remains."""
        trade = self._portfolio.trades.get(trade_id)
        if trade is None:
            raise TradingError(["unknown_trade"])
        if not self._settings.enabled:
            raise TradingError(["trading_disabled"])
        if trade.token in self._busy:
            raise TradingError(["pair_busy"])
        self._busy.add(trade.token)
        try:
            record = self._portfolio.record_for_trade(trade)
            instruments = self._instruments_of(trade, record)
            legs = await self._fresh_legs(trade)
            if legs is None:
                raise TradingError(["positions_unknown"])
            position = legs[0] if side is LegSide.LONG else legs[1]
            closes = []
            if position is not None and position.qty_tokens > 0:
                ticket = self._ticket(trade, record, instruments[side], "fix_leg", side, False, position.qty_tokens, self._clock_ms(), 9)
                await self._send(ticket)
                await self._resolve(ticket)
                closes.append(ticket)
            legs = await self._fresh_legs(trade)
            if legs is not None and all(leg is None for leg in legs):
                await self._collect_fills(trade, closes)
                trade.fees_usd += self._fees_of(closes)
                trade.status = "closed"
                trade.closed_at_ms = int(self._clock_ms())
                trade.close_reason = "leg_failure"
                self._portfolio.finish(trade)
                self._journal.emit(Level.INFO, "trading", "leg_closed_pair_done", trade_id=trade.id, token=trade.token)
                return {"id": str(trade.id), "status": "closed"}
            self._journal.emit(Level.WARNING, "trading", "leg_closed", trade_id=trade.id, token=trade.token, side=side.value)
            return self._portfolio.describe_trade(trade)
        finally:
            self._busy.discard(trade.token)

    async def close_all(self) -> dict[str, Any]:
        targets = [trade.id for trade in self._portfolio.trades.values() if trade.status == "open"]
        results = await asyncio.gather(*(self.close_pair(trade_id, "close_all") for trade_id in targets), return_exceptions=True)
        return {
            "closed": [str(trade_id) for trade_id, result in zip(targets, results) if not isinstance(result, BaseException)],
            "failed": {str(trade_id): str(result) for trade_id, result in zip(targets, results) if isinstance(result, BaseException)},
        }

    # ── views ────────────────────────────────────────────────────────────────

    def _snapshot(self) -> dict[str, Any]:
        tradable = [name for name in self._exchanges.names if name not in READ_ONLY_EXCHANGES]
        return {
            "size_usd": str(self._size_usd()),
            "leverage": {name: self._leverage(name) for name in tradable},
            "isolated": self._settings.isolated,
            "entry_min_roi_pct": str(self._settings.entry_min_roi_pct),
            "taker_fee_pct": {name: str(self._taker_fee_pct(name)) for name in tradable},
        }

    def annotate(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**row, "open_blocks": self.open_blocks_for(row["key"])} for row in rows]

    def status(self) -> dict[str, Any]:
        now = self._clock_ms()
        return {
            "enabled": self._settings.enabled,
            "busy": sorted(self._busy),
            "warmed": len(self._warmed),
            "warm_errors": [
                {"exchange": key[0], "symbol": key[1], "error": error} for key, (_, error) in self._warm_failed.items()
            ],
            "blocked": {exchange: reason for exchange, (until, reason) in self._blocked_until.items() if until > now},
            "settings": self._snapshot()
            | {
                "max_open_pairs": self._settings.max_open_pairs,
                "max_total_usd": str(self._settings.max_total_usd),
                "margin_buffer_pct": str(self._settings.margin_buffer_pct),
            },
        }
