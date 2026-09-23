from decimal import Decimal

from app.config.settings import FeedSettings
from app.core.pairs import Quote, assess_pair
from app.instruments.service import PairRecord
from app.market.state import MarketState
from app.strategies.price_gap.engine import PriceGapEngine
from tests.core.helpers import instrument


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class FakeFeed:
    def __init__(self, clock):
        self.depth: list[str] = []
        self.resubscribed: list[str] = []
        self.clock = clock
        self.alive = True

    def set_depth(self, symbols):
        self.depth = list(symbols)

    def resubscribe_depth(self, symbol):
        self.resubscribed.append(symbol)


class Catalog:
    def __init__(self, records):
        self._records = records

    def records(self):
        return self._records


def sol_record(**flags) -> PairRecord:
    binance = instrument("binance", "SOLUSDT", "SOL", qty_step_units="0.01", min_qty_units="0.01")
    mexc = instrument("mexc", "SOL_USDT", "SOL", qty_unit_tokens="0.1", min_notional_usd="0")
    assessment = assess_pair(binance, mexc, Quote(mark=Decimal("100"), index=Decimal("100")), Quote(mark=Decimal("100"), index=Decimal("100")))
    return PairRecord(key="binance:SOLUSDT|mexc:SOL_USDT", assessment=assessment, **flags)


def build(record: PairRecord, **settings):
    clock = Clock()
    state = MarketState(clock)
    binance, mexc = FakeFeed(clock), FakeFeed(clock)
    radar_symbols = []
    engine = PriceGapEngine(
        Catalog([record]),
        state,
        {"binance": binance, "mexc": mexc},
        # enter_min_samples=1: these tests drive a handful of ticks and are about everything except how many
        # readings the feed demands. That rule has its own tests in tests/core/test_episodes.py.
        FeedSettings(
            **{
                "size_usd": Decimal("1000"),
                "min_roi_pct": Decimal("0.5"),
                "enter_after_ms": 300,
                "enter_min_samples": 1,
                **settings,
            }
        ),
        lambda exchange: Decimal("0.05"),
        lambda instruments: radar_symbols.extend(sorted(item.symbol_raw for item in instruments)),
        clock,
    )
    return engine, state, clock, binance, mexc, radar_symbols


def push_market(state, mexc_ask="100.00", binance_bid="101.20"):
    # MEXC is cheap, Binance is rich: long MEXC, short Binance.
    state.set_top("mexc", "SOL_USDT", float(Decimal(mexc_ask) - Decimal("0.01")), float(mexc_ask), 1)
    state.set_top("binance", "SOLUSDT", float(binance_bid), float(Decimal(binance_bid) + Decimal("0.01")), 1)
    # MEXC quantities are contracts of 0.1 SOL; Binance quantities are SOL.
    state.set_book("mexc", "SOL_USDT", [[float(mexc_ask) - 0.01, 1000, 1]], [[float(mexc_ask), 1000, 1], [float(mexc_ask) + 0.5, 1000, 1]], 1)
    state.set_book("binance", "SOLUSDT", [[binance_bid, "100"], ["100.00", "100"]], [[str(Decimal(binance_bid) + Decimal("0.01")), "100"]], 1)


def test_gap_enters_the_feed_after_holding_above_threshold():
    engine, state, clock, binance, mexc, radar_symbols = build(sol_record())
    engine.tick()  # catalog sync
    assert radar_symbols == ["SOLUSDT", "SOL_USDT"]

    push_market(state)
    engine.tick()  # radar sees the gap, books get chosen
    assert binance.depth == ["SOLUSDT"] and mexc.depth == ["SOL_USDT"]
    assert engine.view()["rows"] == []

    clock.now += 400
    push_market(state)
    engine.tick()

    rows = engine.view()["rows"]
    assert len(rows) == 1
    row = rows[0]
    assert (row["long"]["exchange"], row["short"]["exchange"]) == ("mexc", "binance")
    assert row["qty_tokens"] == "10"  # $1000 / $100, on the common 0.1 SOL step
    assert Decimal(row["roi_net_pct"]) == Decimal("1.2") - Decimal("0.2")
    assert Decimal(row["capacity_usd"]) > 0
    assert row["block"] is None
    assert row["lifetime_ms"] == 400  # counted from the first sighting, not from entering the feed


def test_a_long_wait_keeps_short_gaps_out_and_their_books_in():
    # fast_enter_multiple=0 turns the fast path off: this test is about the plain wait, and the gap it pushes
    # sits right on the fast band, so the two rules would otherwise overlap.
    engine, state, clock, binance, mexc, _ = build(sol_record(), enter_after_ms=45_000, fast_enter_multiple=Decimal(0))
    engine.tick()
    for _ in range(44):
        push_market(state)
        engine.tick()
        clock.now += 1_000
    assert engine.view()["rows"] == []
    assert engine.view()["radar"][0]["phase"] == "candidate"
    assert binance.depth == ["SOLUSDT"]  # the candidate keeps its books while it earns its lifetime

    push_market(state)
    clock.now += 1_000
    engine.tick()
    rows = engine.view()["rows"]
    assert len(rows) == 1 and rows[0]["lifetime_ms"] >= 45_000


def test_stale_leg_blocks_the_row_and_does_not_extend_the_gap():
    engine, state, clock, binance, mexc, _ = build(sol_record())
    engine.tick()
    push_market(state)
    engine.tick()
    clock.now += 400
    push_market(state)
    engine.tick()
    assert len(engine.view()["rows"]) == 1

    clock.now += 1_500  # books stood still; best prices still match them, so they are quiet, not stale
    engine.tick()
    assert engine.view()["rows"][0]["block"] is None

    # The ALT case: Binance best prices move on while its order book does not.
    state.set_top("binance", "SOLUSDT", 100.40, 100.41, 2)
    clock.now += 200
    engine.tick()
    row = engine.view()["rows"][0]
    assert row["block"] == "stale"

    clock.now += 2_100  # no fresh measurement for longer than exit_after_ms
    engine.tick()
    assert engine.view()["rows"] == []


def test_quiet_book_is_trusted_only_up_to_its_limit():
    engine, state, clock, *_ = build(sol_record(), quiet_book_max_ms=3_000)
    engine.tick()
    push_market(state)
    engine.tick()
    clock.now += 400
    push_market(state)
    engine.tick()
    clock.now += 3_500
    engine.tick()
    assert engine.view()["rows"][0]["block"] == "stale"


def test_suspicious_pair_is_shown_but_blocked():
    record = sol_record()
    record.assessment = assess_pair(
        record.assessment.a, record.assessment.b, Quote(mark=Decimal("100"), index=Decimal("100")), Quote(mark=Decimal("100"), index=Decimal("104"))
    )
    engine, state, clock, *_ = build(record)
    engine.tick()
    push_market(state)
    engine.tick()
    clock.now += 400
    push_market(state)
    engine.tick()

    row = engine.view()["rows"][0]
    assert row["suspicious"] is True
    assert row["block"] == "suspicious"


def test_flipped_direction_never_shows_the_pair_twice():
    engine, state, clock, *_ = build(sol_record())
    engine.tick()
    push_market(state)  # long MEXC, short Binance
    engine.tick()
    clock.now += 400
    push_market(state)
    engine.tick()
    assert engine.view()["rows"][0]["long"]["exchange"] == "mexc"

    def flipped():
        # Binance is now the cheap side by more than the threshold.
        state.set_top("binance", "SOLUSDT", 98.79, 98.80, 2)
        state.set_top("mexc", "SOL_USDT", 100.0, 100.01, 2)
        state.set_book("binance", "SOLUSDT", [["98.79", "100"]], [["98.80", "100"]], 2)
        state.set_book("mexc", "SOL_USDT", [[100.0, 1000, 1]], [[100.01, 1000, 1]], 2)

    flipped()
    clock.now += 200
    engine.tick()
    flipped()
    clock.now += 400
    engine.tick()  # new direction entered; old one is still inside its exit hysteresis

    rows = engine.view()["rows"]
    assert len(rows) == 1
    assert rows[0]["long"]["exchange"] == "binance"


def test_no_gap_stays_on_the_radar_only():
    engine, state, clock, *_ = build(sol_record())
    engine.tick()
    push_market(state, mexc_ask="100.00", binance_bid="100.02")
    engine.tick()
    clock.now += 400
    push_market(state, mexc_ask="100.00", binance_bid="100.02")
    engine.tick()

    view = engine.view()
    assert view["rows"] == []
    assert len(view["radar"]) == 1
    assert Decimal(view["radar"][0]["roi_net_pct"]) < 0


def test_large_catalog_radar_is_refreshed_a_slice_per_tick():
    from app.strategies.price_gap import engine as engine_module

    count = engine_module.RADAR_FULL_PASS_PAIRS + 100
    quote = Quote(mark=Decimal("100"), index=Decimal("100"))
    records = []
    for index in range(count):
        a, b = instrument("binance", f"T{index}USDT", f"T{index}"), instrument("mexc", f"T{index}_USDT", f"T{index}")
        records.append(PairRecord(key=f"binance:T{index}USDT|mexc:T{index}_USDT", assessment=assess_pair(a, b, quote, quote)))
    clock = Clock()
    state = MarketState(clock)
    catalog = Catalog(records)
    engine = PriceGapEngine(catalog, state, {"binance": FakeFeed(clock), "mexc": FakeFeed(clock)}, FeedSettings(), lambda exchange: Decimal("0.05"),
                            lambda instruments: None, clock)
    engine.tick()
    for index in range(count):
        state.set_top("binance", f"T{index}USDT", 100.0, 100.1, 1)
        state.set_top("mexc", f"T{index}_USDT", 100.5, 100.6, 1)

    seen = []
    for _ in range(engine_module.RADAR_SLICES):
        engine.tick()
        seen.append(len(engine._top_by_key))
    size = -(-count // engine_module.RADAR_SLICES)
    assert seen == [size * step for step in range(1, engine_module.RADAR_SLICES)] + [count]

    catalog._records = records[:10]  # pairs that leave the catalog leave the radar at once
    engine.tick()
    assert set(engine._top_by_key) <= {record.key for record in records[:10]}


def test_rows_carry_profit_funding_result_and_interest():
    from app.core.funding import FundingRate

    clock = Clock()
    state = MarketState(clock)
    rates = {
        ("mexc", "SOL_USDT"): FundingRate(Decimal("0.01"), Decimal(8), int(clock.now) + 600_000),  # the long pays once in 8 h
        ("binance", "SOLUSDT"): FundingRate(Decimal("-0.02"), Decimal(4), int(clock.now) + 3_600_000),  # the short pays twice
    }
    engine = PriceGapEngine(
        Catalog([sol_record()]), state, {"binance": FakeFeed(clock), "mexc": FakeFeed(clock)},
        FeedSettings(size_usd=Decimal("1000"), min_roi_pct=Decimal("0.5"), enter_after_ms=300, enter_min_samples=1),
        lambda exchange: Decimal("0.05"),
        lambda instruments: None, clock, funding=lambda exchange, symbol: rates.get((exchange, symbol)),
    )
    engine.tick()
    push_market(state)
    engine.tick()
    clock.now += 400
    push_market(state)
    engine.tick()

    row = engine.view()["rows"][0]
    assert Decimal(row["profit_usd"]) == Decimal("10")  # 1.0 % of $1000
    # next_ms per leg: the feed shows both settlement timers, because the legs rarely settle together.
    assert row["funding"]["long"] == {"rate_pct": "0.01", "interval_h": "8", "next_ms": rates[("mexc", "SOL_USDT")].next_ms}
    assert row["funding"]["short"]["next_ms"] == rates[("binance", "SOLUSDT")].next_ms
    assert Decimal(row["funding"]["horizon_pct"]) == Decimal("-0.01") - Decimal("0.04")
    assert Decimal(row["funding"]["horizon_usd"]) == Decimal("-0.5")
    assert Decimal(row["funding"]["next_pct"]) == Decimal("-0.01")
    assert Decimal(row["funding"]["next_usd"]) == Decimal("-0.1")
    assert Decimal(row["total_pct"]) == Decimal("0.95") and Decimal(row["total_usd"]) == Decimal("9.5")
    assert row["funding_known"] is True
    assert row["score"] == sum(row["score_parts"].values()) and row["score"] > 0


def test_radar_lists_coins_with_their_pairs():
    from app.strategies.price_gap import engine as engine_module

    quote = Quote(mark=Decimal("100"), index=Decimal("100"))
    records = []
    exchanges = ["binance", "mexc", "gate", "aster", "bingx"]
    for coin in ("AAA", "BBB", "CCC"):
        for index, a in enumerate(exchanges):
            for b in exchanges[index + 1 :]:
                left, right = instrument(a, f"{coin}-{a}", coin), instrument(b, f"{coin}-{b}", coin)
                records.append(PairRecord(key=f"{a}:{coin}|{b}:{coin}", assessment=assess_pair(left, right, quote, quote)))
    clock = Clock()
    state = MarketState(clock)
    engine = PriceGapEngine(Catalog(records), state, {name: FakeFeed(clock) for name in exchanges},
                            FeedSettings(radar_rows=2), lambda exchange: Decimal("0.05"), lambda instruments: None, clock)
    engine.tick()
    for spread, coin in ((3.0, "AAA"), (2.0, "BBB"), (1.0, "CCC")):
        for position, exchange in enumerate(exchanges):
            price = 100 + spread * position / 10
            state.set_top(exchange, f"{coin}-{exchange}", price, price + 0.01, 1)
    engine.tick()

    radar = engine.view()["radar"]
    coins = [row["token"] for row in radar]
    assert set(coins) == {"AAA", "BBB"}  # two coins, the widest spreads
    assert coins.count("AAA") == 10 == engine_module.RADAR_PAIRS_PER_TOKEN  # every pair of a coin on five exchanges


def test_unchanged_books_are_not_walked_again(monkeypatch):
    from app.strategies.price_gap import engine as engine_module

    walks = []
    original = engine_module.best_entry
    monkeypatch.setattr(engine_module, "best_entry", lambda *args: walks.append(1) or original(*args))
    engine, state, clock, *_ = build(sol_record(), min_roi_pct=Decimal("5"))  # below the threshold: a radar row, no gap
    engine.tick()
    push_market(state)
    engine.tick()  # books chosen and measured
    assert len(walks) == 1
    first = engine.view()["radar"][0]

    clock.now += 100
    engine.tick()  # the same book objects: the numbers and the row are reused
    assert len(walks) == 1
    assert engine.view()["radar"][0] is first

    push_market(state, binance_bid="101.50")
    clock.now += 100
    engine.tick()
    assert len(walks) == 2
    assert Decimal(engine.view()["radar"][0]["roi_net_pct"]) == Decimal("1.5") - Decimal("0.2")


def test_tradable_pairs_take_books_before_suspicious_ones():
    normal = Quote(mark=Decimal("100"), index=Decimal("100"))
    records = []
    for coin, index_b in (("AAA", "104"), ("BBB", "100"), ("CCC", "100")):  # AAA has the widest gap but a suspicious index
        a, b = instrument("binance", f"{coin}USDT", coin), instrument("mexc", f"{coin}_USDT", coin)
        assessment = assess_pair(a, b, normal, Quote(mark=Decimal("100"), index=Decimal(index_b)))
        records.append(PairRecord(key=f"binance:{coin}USDT|mexc:{coin}_USDT", assessment=assessment))
    clock = Clock()
    state = MarketState(clock)
    binance, mexc = FakeFeed(clock), FakeFeed(clock)
    engine = PriceGapEngine(Catalog(records), state, {"binance": binance, "mexc": mexc},
                            FeedSettings(book_limit=2, min_roi_pct=Decimal("0.5")), lambda exchange: Decimal("0.05"),
                            lambda instruments: None, clock)
    engine.tick()
    for coin, bid in (("AAA", 103.0), ("BBB", 102.0), ("CCC", 101.5)):
        state.set_top("mexc", f"{coin}_USDT", 99.99, 100.0, 1)
        state.set_top("binance", f"{coin}USDT", bid, bid + 0.01, 1)
    engine.tick()

    assert records[0].suspicious and not records[1].suspicious
    assert sorted(binance.depth) == ["BBBUSDT", "CCCUSDT"]


def test_radar_coins_from_tradable_pairs_come_before_suspicious_spreads():
    normal = Quote(mark=Decimal("100"), index=Decimal("100"))
    records = []
    for coin, index_b in (("AAA", "104"), ("BBB", "100"), ("CCC", "100")):
        a, b = instrument("binance", f"{coin}USDT", coin), instrument("mexc", f"{coin}_USDT", coin)
        records.append(PairRecord(key=f"binance:{coin}USDT|mexc:{coin}_USDT", assessment=assess_pair(a, b, normal, Quote(mark=Decimal("100"), index=Decimal(index_b)))))
    clock = Clock()
    state = MarketState(clock)
    engine = PriceGapEngine(Catalog(records), state, {"binance": FakeFeed(clock), "mexc": FakeFeed(clock)},
                            FeedSettings(radar_rows=2, min_roi_pct=Decimal("5")), lambda exchange: Decimal("0.05"),
                            lambda instruments: None, clock)
    engine.tick()
    for coin, bid in (("AAA", 103.0), ("BBB", 102.0), ("CCC", 101.5)):
        state.set_top("mexc", f"{coin}_USDT", 99.99, 100.0, 1)
        state.set_top("binance", f"{coin}USDT", bid, bid + 0.01, 1)
    engine.tick()
    assert [row["token"] for row in engine.view()["radar"]] == ["BBB", "CCC"]

    engine.apply_settings(FeedSettings(radar_rows=3, min_roi_pct=Decimal("5")))
    engine.tick()
    assert [row["token"] for row in engine.view()["radar"]] == ["BBB", "CCC", "AAA"]
