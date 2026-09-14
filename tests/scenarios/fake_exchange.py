"""
VFP: A scriptable exchange for failure scenarios — fills, rejections, partial fills, lost answers — that keeps positions like a real one.
Changes when: execution needs a new kind of exchange behaviour to be tested.
Anti-goal:
1. Network, clocks or randomness — every behaviour is scripted by the test.
"""

from __future__ import annotations

from decimal import Decimal

from app.core.legs import OrderOutcome
from app.core.schemas import Instrument, LegSide
from app.exchanges.base import Balance, FillReport, OrderReport, Position


class FakeExchange:
    """
    script: behaviours consumed per order in sending order —
    "fill", "reject", "partial:0.6", "lost_filled" (answer lost, order filled), "lost_missing" (answer lost, never placed).
    """

    def __init__(self, name: str, prices: dict[str, Decimal], fee_rate: Decimal = Decimal("0.0005")):
        self.name = name
        self.prices = prices
        self.fee_rate = fee_rate
        self.script: list[str] = []
        self.positions: dict[tuple[str, LegSide], Position] = {}
        self.orders: dict[str, OrderReport] = {}
        self.sent: list[dict] = []
        self.prepared: list[tuple[str, int, bool]] = []
        self.funding = Decimal("0.05")
        self.available = Decimal("10000")

    # ── helpers ──────────────────────────────────────────────────────────────

    def _apply(self, instrument: Instrument, leg: LegSide, opening: bool, tokens: Decimal) -> None:
        key = (instrument.symbol_raw, leg)
        current = self.positions.get(key)
        held = current.qty_tokens if current else Decimal(0)
        price = self.prices[instrument.symbol_raw]
        new_qty = held + tokens if opening else max(Decimal(0), held - tokens)
        if new_qty == 0:
            self.positions.pop(key, None)
            return
        entry = current.entry_price if current else price
        self.positions[key] = Position(self.name, instrument.symbol_raw, instrument.token, leg, new_qty, entry, price, None, 3, "isolated")

    def _report(self, cid: str, outcome: OrderOutcome, requested: Decimal, filled: Decimal, price: Decimal | None, status: str) -> OrderReport:
        return OrderReport(
            client_order_id=cid,
            exchange_order_id=f"{self.name}-{cid}" if outcome is not OrderOutcome.REJECTED else None,
            outcome=outcome,
            status=status,
            requested_tokens=requested,
            filled_tokens=filled,
            avg_price=price,
            fee_usd=None,
            error_code="-2019" if outcome is OrderOutcome.REJECTED else None,
            error_message="Margin is insufficient." if outcome is OrderOutcome.REJECTED else None,
            sent_ts_ms=1,
            ack_ts_ms=2,
        )

    # ── adapter contract ─────────────────────────────────────────────────────

    async def prepare_symbol(self, instrument: Instrument, leverage: int, isolated: bool) -> None:
        self.prepared.append((instrument.symbol_raw, leverage, isolated))

    async def place_market_order(self, instrument, leg, opening, qty_units, client_order_id, leverage, isolated) -> OrderReport:
        behaviour = self.script.pop(0) if self.script else "fill"
        requested = qty_units * instrument.qty_unit_tokens
        price = self.prices[instrument.symbol_raw]
        self.sent.append({"cid": client_order_id, "leg": leg, "opening": opening, "units": qty_units, "behaviour": behaviour})
        if behaviour == "reject":
            report = self._report(client_order_id, OrderOutcome.REJECTED, requested, Decimal(0), None, "REJECTED")
            return report
        if behaviour.startswith("partial:"):
            filled = (requested * Decimal(behaviour.split(":")[1])).quantize(instrument.qty_step_tokens)
            self._apply(instrument, leg, opening, filled)
            report = self._report(client_order_id, OrderOutcome.PARTIAL, requested, filled, price, "EXPIRED")
            self.orders[client_order_id] = report
            return report
        if behaviour == "lost_filled":
            self._apply(instrument, leg, opening, requested)
            self.orders[client_order_id] = self._report(client_order_id, OrderOutcome.FILLED, requested, requested, price, "FILLED")
            return OrderReport(client_order_id, None, OrderOutcome.UNKNOWN, "error", requested, Decimal(0), None, None, "RequestTimeout", "timeout", 1, None)
        if behaviour == "lost_missing":
            return OrderReport(client_order_id, None, OrderOutcome.UNKNOWN, "error", requested, Decimal(0), None, None, "RequestTimeout", "timeout", 1, None)
        self._apply(instrument, leg, opening, requested)
        report = self._report(client_order_id, OrderOutcome.FILLED, requested, requested, price, "FILLED")
        self.orders[client_order_id] = report
        return report

    async def fetch_order(self, instrument: Instrument, client_order_id: str, requested_tokens: Decimal) -> OrderReport:
        report = self.orders.get(client_order_id)
        if report is None:
            return OrderReport(client_order_id, None, OrderOutcome.REJECTED, "not_found", requested_tokens, Decimal(0), None, None, "-2013", "Order does not exist.", 1, None)
        return report

    async def fetch_fills(self, instrument: Instrument, report: OrderReport) -> list[FillReport]:
        if report.filled_tokens <= 0:
            return []
        fee = report.filled_tokens * report.avg_price * self.fee_rate
        return [FillReport(f"fill-{report.client_order_id}", 3, report.avg_price, report.filled_tokens, fee, "USDT", False)]

    async def fetch_positions(self, instruments) -> list[Position]:
        return [position for position in self.positions.values() if position.symbol_raw in instruments]

    async def fetch_balance(self) -> Balance:
        return Balance(self.name, self.available, self.available, Decimal(0))

    async def fetch_funding_usd(self, symbol_raw: str, since_ms: int) -> Decimal:
        return self.funding
