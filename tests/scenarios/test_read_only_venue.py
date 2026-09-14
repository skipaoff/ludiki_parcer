from decimal import Decimal

from app.core.pairs import Quote, assess_pair
from app.instruments.service import PairRecord
from tests.core.helpers import instrument
from tests.scenarios.test_trading_scenarios import PAIR_KEY, harness


async def test_pair_with_a_read_only_venue_is_refused_before_anything_else():
    h = await harness()
    gate_sol = instrument("gate", "SOL_USDT", "SOL", qty_unit_tokens="1", min_notional_usd="0")
    var_sol = instrument("variational", "SOL", "SOL", qty_step_units="0.00000001", min_qty_units="0", min_notional_usd="0")
    record = PairRecord(
        key="gate:SOL_USDT|variational:SOL",
        assessment=assess_pair(gate_sol, var_sol, Quote(mark=Decimal("100"), index=Decimal("100")), Quote(mark=Decimal("100"))),
    )
    h.engine.record = record
    h.engine.set_quote(long=var_sol, short=gate_sol)

    blocks = h.execution.open_blocks_for(PAIR_KEY)

    assert blocks == ["exchange_read_only:variational"]
    assert h.mexc.sent == [] and h.binance.sent == []
