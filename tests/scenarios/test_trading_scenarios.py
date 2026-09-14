"""Failure scenarios of PLAN.md sections 9.3, 9.4 and 15 on a scripted exchange."""

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.config.settings import PortfolioSettings, TradingSettings
from app.core.schemas import LegSide
from app.execution.service import ExecutionService, TradingError
from app.journal.journal import Journal
from app.market.state import MarketState
from app.portfolio.service import PortfolioService, _ExchangeState
from tests.scenarios.fake_exchange import FakeExchange
from tests.strategies.test_price_gap_engine import Catalog, sol_record

PAIR_KEY = "binance:SOLUSDT|mexc:SOL_USDT"


class Clock:
    def __init__(self):
        self.now = 1_789_300_000_000.0

    def __call__(self):
        return self.now


class FakeExchanges:
    def __init__(self, adapters):
        self.adapters = adapters

    @property
    def names(self):
        return list(self.adapters)

    def snapshot(self):
        return [{"name": name, "keys": "ok"} for name in self.adapters]

    def adapter(self, name):
        return self.adapters[name]

    def describe_one(self, name):
        return {"check": {"accepted": True}}


class FakeEngine:
    quote_max_age_ms = 600

    def __init__(self, record, clock):
        self.record = record
        self.clock = clock
        self.quote = None

    def set_quote(self, **overrides):
        values = dict(
            long=self.record.assessment.b,  # MEXC
            short=self.record.assessment.a,  # Binance
            qty_tokens=Decimal("10"),
            long_avg=Decimal("100"),
            short_avg=Decimal("101.2"),
            roi_net_pct=Decimal("1.0"),
            exit_spread_pct=Decimal("0.2"),
            problem=None,
            ts_ms=int(self.clock()),
        )
        values.update(overrides)
        self.quote = SimpleNamespace(**values)

    def current_quote(self, key):
        return None if self.quote is None or key != PAIR_KEY else (self.record, self.quote, None)

    def view(self):
        return {"rows": [{"key": PAIR_KEY}]}

    def episode_state(self, key):
        return None


class NullRecorder:
    def episode_id(self, key):
        return None


async def no_sleep(seconds):
    return None


async def harness(enabled=True):
    clock = Clock()
    record = sol_record(pair_id=5, instrument_a_id=1, instrument_b_id=2)
    binance = FakeExchange("binance", {"SOLUSDT": Decimal("101.2")})
    mexc = FakeExchange("mexc", {"SOL_USDT": Decimal("100")})
    exchanges = FakeExchanges({"binance": binance, "mexc": mexc})
    rows, events = [], []
    journal = Journal()
    journal.add_sink(events.append)
    state = MarketState(clock)
    state.set_instruments([record.assessment.a, record.assessment.b])
    portfolio = PortfolioService(
        exchanges, Catalog([record]), state, SimpleNamespace(ready=False), lambda t, r: rows.append((t, r)), journal,
        PortfolioSettings(), lambda exchange: Decimal("0.05"), clock_ms=clock,
    )
    for name, exchange in (("binance", binance), ("mexc", mexc)):
        portfolio._accounts[name] = _ExchangeState(balance=await exchange.fetch_balance(), balance_ms=clock())
    engine = FakeEngine(record, clock)
    engine.set_quote()
    execution = ExecutionService(
        TradingSettings(enabled=enabled, status_query_attempts=3),
        lambda: Decimal("1000"),
        exchanges,
        engine,
        portfolio,
        NullRecorder(),
        lambda t, r: rows.append((t, r)),
        journal,
        lambda exchange: Decimal("0.05"),
        balance_max_age_ms=120_000,
        clock_ms=clock,
        sleep=no_sleep,
    )
    for instrument in (record.assessment.a, record.assessment.b):
        await execution.warm(instrument)
    return SimpleNamespace(
        clock=clock, record=record, binance=binance, mexc=mexc, portfolio=portfolio, engine=engine,
        execution=execution, rows=rows, events=events,
    )


def critical(h):
    return [event.type for event in h.events if event.level.value == "critical"]


def table(h, name):
    return [row for t, row in h.rows if t == name]


async def test_both_legs_fill_and_the_pair_opens():
    h = await harness()

    card = await h.execution.open_pair(PAIR_KEY)

    assert card["status"] == "open"
    trade = h.portfolio.trades[int(card["id"])]
    assert trade.qty_tokens == Decimal("10")
    assert (trade.entry_long_avg, trade.entry_short_avg) == (Decimal("100"), Decimal("101.2"))
    assert trade.roi_actual_entry == Decimal("1.2") - Decimal("0.2")
    assert trade.fees_usd == Decimal("10") * Decimal("100") * Decimal("0.0005") + Decimal("10") * Decimal("101.2") * Decimal("0.0005")
    assert h.mexc.positions[("SOL_USDT", LegSide.LONG)].qty_tokens == Decimal("10")
    assert h.binance.positions[("SOLUSDT", LegSide.SHORT)].qty_tokens == Decimal("10")
    assert [sent["units"] for sent in h.mexc.sent] == [Decimal("100")]  # 10 SOL in 0.1 SOL contracts
    assert len(table(h, "fills")) == 2
    assert critical(h) == []


async def test_rejected_leg_is_hedged_by_closing_the_filled_one():
    h = await harness()
    h.binance.script = ["reject"]

    card = await h.execution.open_pair(PAIR_KEY)

    assert card["status"] == "leg_failed"
    assert h.mexc.positions == {} and h.binance.positions == {}
    closing = h.mexc.sent[-1]
    assert closing["opening"] is False and closing["units"] == Decimal("100")
    assert critical(h) == ["leg_rejected_hedge_closed"]
    assert h.portfolio.trades == {}


async def test_partial_fill_equalizes_to_the_smaller_leg():
    h = await harness()
    h.mexc.script = ["partial:0.6"]

    card = await h.execution.open_pair(PAIR_KEY)

    assert card["status"] == "open"
    assert Decimal(card["qty_tokens"]) == Decimal("6")
    assert h.binance.positions[("SOLUSDT", LegSide.SHORT)].qty_tokens == Decimal("6")
    assert h.binance.sent[-1]["opening"] is False and h.binance.sent[-1]["units"] == Decimal("4")
    assert "partial_fill_equalized" in [event.type for event in h.events]


async def test_lost_answer_is_resolved_by_status_query():
    h = await harness()
    h.mexc.script = ["lost_filled"]

    card = await h.execution.open_pair(PAIR_KEY)

    assert card["status"] == "open"
    assert critical(h) == []


async def test_order_that_never_reached_the_exchange_counts_as_rejected_only_after_repeated_not_found():
    h = await harness()
    h.binance.script = ["lost_missing"]

    card = await h.execution.open_pair(PAIR_KEY)

    assert card["status"] == "leg_failed"
    assert h.mexc.positions == {}
    assert critical(h) == ["leg_rejected_hedge_closed"]


async def test_failed_hedge_leaves_the_pair_visible_as_leg_lost():
    h = await harness()
    h.binance.script = ["reject"]
    h.mexc.script = ["fill", "reject"]  # the open fills, the hedge close is refused

    card = await h.execution.open_pair(PAIR_KEY)

    assert card["status"] == "leg_lost"
    assert h.mexc.positions[("SOL_USDT", LegSide.LONG)].qty_tokens == Decimal("10")
    assert "hedge_fix_failed" in critical(h)
    assert int(card["id"]) in h.portfolio.trades


async def test_close_uses_real_positions_and_settles_pnl():
    h = await harness()
    card = await h.execution.open_pair(PAIR_KEY)
    h.mexc.prices["SOL_USDT"] = Decimal("101")
    h.binance.prices["SOLUSDT"] = Decimal("101.1")

    result = await h.execution.close_pair(int(card["id"]))

    assert result["status"] == "closed"
    assert h.mexc.positions == {} and h.binance.positions == {}
    final = table(h, "trades")[-1]
    assert final["status"] == "closed" and final["close_reason"] == "manual"
    assert final["exit_long_avg"] == Decimal("101") and final["exit_short_avg"] == Decimal("101.1")
    # long +10, short +1, fees in and out, funding 0.05 on each exchange
    fees = Decimal("10") * (Decimal("100") + Decimal("101.2") + Decimal("101") + Decimal("101.1")) * Decimal("0.0005")
    assert final["pnl_net_usd"] == Decimal("11") - fees + Decimal("0.10")
    assert len(table(h, "funding_payments")) == 2
    assert h.portfolio.trades == {}


async def test_close_that_keeps_failing_ends_in_leg_lost():
    h = await harness()
    card = await h.execution.open_pair(PAIR_KEY)
    h.binance.script = ["reject", "reject", "reject"]

    with pytest.raises(TradingError, match="leg_close_failed"):
        await h.execution.close_pair(int(card["id"]))

    trade = h.portfolio.trades[int(card["id"])]
    assert trade.status == "leg_lost"
    assert h.mexc.positions == {}
    assert h.binance.positions[("SOLUSDT", LegSide.SHORT)].qty_tokens == Decimal("10")
    assert "leg_close_failed" in critical(h)

    h.binance.script = ["fill"]
    result = await h.execution.close_leg(trade.id, LegSide.SHORT)
    assert result["status"] == "closed"
    assert h.portfolio.trades == {}


async def test_nothing_is_sent_while_trading_is_disabled():
    h = await harness(enabled=False)
    with pytest.raises(TradingError) as blocked:
        await h.execution.open_pair(PAIR_KEY)
    assert "trading_disabled" in blocked.value.reasons
    assert h.mexc.sent == [] and h.binance.sent == []


async def test_pre_trade_checks_block_before_any_order():
    h = await harness()
    h.engine.set_quote(roi_net_pct=Decimal("0.3"))
    h.portfolio._accounts["binance"] = replace(h.portfolio._accounts["binance"], balance=replace(await h.binance.fetch_balance(), available_usd=Decimal("100")))

    with pytest.raises(TradingError) as blocked:
        await h.execution.open_pair(PAIR_KEY)

    assert blocked.value.reasons == ["roi_below_entry", "insufficient_margin:short"]
    assert h.mexc.sent == [] and h.binance.sent == []


async def test_one_pair_per_token_and_stale_quote():
    h = await harness()
    await h.execution.open_pair(PAIR_KEY)
    h.clock.now += 5_000

    assert set(h.execution.open_blocks_for(PAIR_KEY)) >= {"token_already_open", "stale"}
