"""Aster and BingX on public answers recorded 14.09.2026; private shapes follow the documentation and must be confirmed by the trial trade."""

import gzip
import json
from decimal import Decimal
from pathlib import Path

import orjson
import pytest

from app.core.legs import OrderOutcome
from app.core.links import trade_url
from app.core.pairs import assess_pair
from app.core.qty import plan_quantity
from app.core.schemas import LegSide
from app.exchanges.aster import adapter as aster
from app.exchanges.binance import adapter as binance
from app.exchanges.bingx import adapter as bingx
from app.market import bingx_market
from app.market.binance_streams import ALL_BOOK_TICKERS_STREAM, aster_streams, handle_frame
from app.market.state import MarketState

FIXTURES = Path(__file__).parent / "fixtures"
SIGNER_KEY = "0x" + "11" * 32
SIGNER_ADDRESS = "0x19e7e376e7c213b7e7e7e46cc70a5dd086daff2a"  # the address of SIGNER_KEY
MAIN_WALLET = "0x" + "ab" * 20


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def aster_instruments():
    return {item.symbol_raw: item for item in binance.parse_instruments(load("aster_exchangeinfo"), "aster")}


def bingx_instruments():
    return {item.symbol_raw: item for item in bingx.parse_instruments(load("bingx_contracts")["data"])}


# ── Aster ────────────────────────────────────────────────────────────────────


def test_aster_keeps_trading_usdt_perpetuals_in_binance_units():
    items = aster_instruments()
    assert set(items) == {"BTCUSDT", "ETHUSDT", "DOGEUSDT", "1000PEPEUSDT"}  # settling, USD1 and pending contracts are left out
    btc, pepe = items["BTCUSDT"], items["1000PEPEUSDT"]
    assert btc.exchange == "aster"
    assert (btc.qty_step_units, btc.min_qty_units, btc.max_market_qty_units, btc.min_notional_usd) == (
        Decimal("0.001"), Decimal("0.001"), Decimal("120"), Decimal("5"))
    assert (pepe.token, pepe.qty_unit_tokens, pepe.price_unit_tokens) == ("PEPE", 1000, 1000)


def test_aster_quotes_join_book_premium_and_volume():
    quotes = binance.parse_quotes(load("aster_premiumindex"), load("aster_bookticker"), load("aster_ticker24h"))
    book = next(item for item in load("aster_bookticker") if item["symbol"] == "BTCUSDT")
    btc = quotes["BTCUSDT"]
    assert (btc.bid, btc.ask) == (Decimal(book["bidPrice"]), Decimal(book["askPrice"]))
    assert btc.index is not None and btc.volume24h_usd is not None


def test_aster_keys_are_a_wallet_address_and_an_api_wallet_key():
    assert aster.valid_keys(MAIN_WALLET, SIGNER_KEY)
    assert aster.valid_keys(MAIN_WALLET, SIGNER_KEY[2:])
    assert not aster.valid_keys("binance-style-key-123", SIGNER_KEY)
    assert not aster.valid_keys(MAIN_WALLET, "0x1234")


async def test_aster_requests_are_signed_by_the_api_wallet_for_the_main_wallet():
    adapter = aster.AsterAdapter(MAIN_WALLET, SIGNER_KEY)
    try:
        assert adapter.signer_address.lower() == SIGNER_ADDRESS
        signed = adapter._client.sign("v3/order", "fapiPrivate", "POST", {"symbol": "BTCUSDT", "side": "BUY"})
        assert signed["url"] == "https://fapi.asterdex.com/fapi/v3/order"
        body = dict(part.split("=", 1) for part in signed["body"].split("&"))
        assert body["user"] == MAIN_WALLET and body["signer"].lower() == SIGNER_ADDRESS
        assert body["symbol"] == "BTCUSDT" and body["signature"].startswith("0x") and len(body["nonce"]) == 16
    finally:
        await adapter.close()


class Recorder:
    """Stands in for ccxt implicit methods: records the call and returns a canned answer."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __getattr__(self, name):
        async def call(params=None):
            self.calls.append((name, params))
            answer = self.answers[name]
            if isinstance(answer, Exception):
                raise answer
            return answer(params) if callable(answer) else answer

        return call


async def test_aster_account_check_refuses_the_main_wallet_key():
    adapter = aster.AsterAdapter(SIGNER_ADDRESS, SIGNER_KEY)  # the key's own address as the main wallet
    await adapter.close()
    adapter._probe_client = Recorder({"fapipublic_get_v1_time": {"serverTime": 0}})
    adapter._client = Recorder({
        "fapiprivate_get_v3_positionside_dual": {"dualSidePosition": False},
        "fapiprivate_get_v3_balance": [{"asset": "USDT", "balance": "120.5", "availableBalance": "100"}],
        "fapiprivate_get_v3_commissionrate": {"symbol": "BTCUSDT", "makerCommissionRate": "0.0001", "takerCommissionRate": "0.00035"},
    })
    adapter._client.options = {"signerAddress": SIGNER_ADDRESS}
    facts = await adapter.check_account()
    assert facts.permissions.withdrawals is True
    assert facts.one_way_position_mode is True and facts.available_usdt == Decimal("100")
    assert facts.taker_fee_pct == Decimal("0.035")


async def test_aster_market_order_uses_v3_with_client_id_and_reduce_only_close():
    adapter = aster.AsterAdapter(MAIN_WALLET, SIGNER_KEY)
    await adapter.close()
    pepe = aster_instruments()["1000PEPEUSDT"]
    answer = {"orderId": 9, "status": "FILLED", "executedQty": "300", "avgPrice": "0.0034425", "clientOrderId": "lkabccl0"}
    adapter._client = Recorder({"fapiprivate_post_v3_order": answer})
    report = await adapter.place_market_order(pepe, LegSide.LONG, False, Decimal("300"), "lkabccl0", 3, True)
    name, params = adapter._client.calls[0]
    assert name == "fapiprivate_post_v3_order"
    assert params == {"symbol": "1000PEPEUSDT", "side": "SELL", "type": "MARKET", "quantity": "300",
                      "newClientOrderId": "lkabccl0", "newOrderRespType": "RESULT", "reduceOnly": "true"}
    assert report.outcome is OrderOutcome.FILLED and report.filled_tokens == Decimal("300000")
    assert report.avg_price == Decimal("0.0000034425")


def test_aster_streams_take_radar_prices_from_the_all_symbols_stream():
    state = MarketState(lambda: 1.0)
    state.set_instruments(list(aster_instruments().values()))
    frame = {"stream": "!bookTicker", "data": {"e": "bookTicker", "u": 1, "s": "1000PEPEUSDT", "b": "0.0034420", "B": "10",
                                               "a": "0.0034429", "A": "5", "T": 1789375567350, "E": 1789375567351}}
    handle_frame(state, orjson.dumps(frame), "aster")
    top = state.tops[("aster", "1000PEPEUSDT")]
    assert abs(top.bid - 0.000003442) < 1e-15
    streams = aster_streams(state)
    streams.set_radar(["BTCUSDT", "ETHUSDT"])
    assert [socket.desired for socket in streams._radar] == [{ALL_BOOK_TICKERS_STREAM}]
    assert streams.stats()["radar_streams"] == 2


# ── BingX ────────────────────────────────────────────────────────────────────


def test_bingx_contract_steps_minimums_and_multipliers():
    items = bingx_instruments()
    assert set(items) == {"BTC-USDT", "SOL-USDT", "DOGE-USDT", "1000PEPE-USDT", "1000000MOG-USDT"}  # closed, API-disabled and USDC left out
    btc, sol, pepe, mog = items["BTC-USDT"], items["SOL-USDT"], items["1000PEPE-USDT"], items["1000000MOG-USDT"]
    assert (btc.qty_step_units, btc.min_qty_units, btc.min_notional_usd, btc.price_tick) == (Decimal("0.0001"), Decimal("0.0001"), 2, Decimal("0.1"))
    assert sol.qty_step_units == Decimal("0.01")  # quantityPrecision decides, not the "size" field (1 for SOL)
    assert (pepe.token, pepe.qty_unit_tokens, pepe.qty_step_units) == ("PEPE", 1000, 1)
    assert (mog.token, mog.qty_unit_tokens) == ("MOG", 1_000_000)


def test_bingx_quotes_join_ticker_and_premium_index():
    ticker = load("bingx_ticker")["data"]
    premium = load("bingx_premiumindex")["data"]
    quote = bingx.parse_quotes(ticker, premium)["BTC-USDT"]
    row = next(item for item in ticker if item["symbol"] == "BTC-USDT")
    mark = next(item for item in premium if item["symbol"] == "BTC-USDT")
    assert (quote.bid, quote.ask, quote.volume24h_usd) == (Decimal(row["bidPrice"]), Decimal(row["askPrice"]), Decimal(row["quoteVolume"]))
    assert (quote.mark, quote.index) == (Decimal(mark["markPrice"]), Decimal(mark["indexPrice"]))


def test_bingx_pair_with_aster_is_the_same_asset():
    items = bingx_instruments()
    quotes_bingx = bingx.parse_quotes(load("bingx_ticker")["data"], load("bingx_premiumindex")["data"])
    quotes_aster = binance.parse_quotes(load("aster_premiumindex"), load("aster_bookticker"), load("aster_ticker24h"))
    pepe_aster = aster_instruments()["1000PEPEUSDT"]
    assessment = assess_pair(pepe_aster, items["1000PEPE-USDT"], quotes_aster["1000PEPEUSDT"], quotes_bingx["1000PEPE-USDT"])
    assert assessment.suspicious_reason is None
    plan = plan_quantity(Decimal("100"), Decimal("0.0000034420"), long=pepe_aster, short=items["1000PEPE-USDT"])
    assert plan.qty_tokens % 1000 == 0


def test_bingx_positions_side_from_position_side_or_sign():
    sol = bingx_instruments()["SOL-USDT"]
    rows = [
        {"symbol": "SOL-USDT", "positionSide": "SHORT", "positionAmt": "1.50", "avgPrice": "101.5", "markPrice": "101.4",
         "liquidationPrice": 0, "leverage": 3, "isolated": True, "updateTime": 1789376000000},
        {"symbol": "SOL-USDT", "positionSide": "BOTH", "positionAmt": "-0.20", "avgPrice": "101.5", "isolated": False},
        {"symbol": "SOL-USDT", "positionSide": "LONG", "positionAmt": "0"},
        {"symbol": "XRP-USDT", "positionSide": "LONG", "positionAmt": "5"},
    ]
    first, second = bingx.parse_positions(rows, {"SOL-USDT": sol})
    assert (first.side, first.qty_tokens, first.liquidation_price, first.margin_mode, first.leverage) == (LegSide.SHORT, Decimal("1.50"), None, "isolated", 3)
    assert (second.side, second.qty_tokens, second.margin_mode) == (LegSide.SHORT, Decimal("0.20"), "cross")


def test_bingx_balance_funding_fees_and_permissions():
    raw = [{"asset": "USDC", "balance": "5"}, {"asset": "USDT", "balance": "194.8212", "equity": "196.7431", "availableMargin": "193.7609",
                                               "usedMargin": "1.0602", "freezedMargin": "0.5000"}]
    balance = bingx.parse_balance(raw)
    assert (balance.equity_usd, balance.available_usd, balance.margin_used_usd) == (Decimal("196.7431"), Decimal("193.7609"), Decimal("1.5602"))
    assert bingx.parse_usdt_balance(raw) == (Decimal("194.8212"), Decimal("193.7609"))
    income = [{"symbol": "SOL-USDT", "incomeType": "FUNDING_FEE", "income": "-0.0292", "time": 1789376000000},
              {"symbol": "SOL-USDT", "incomeType": "REALIZED_PNL", "income": "3", "time": 1789376000000},
              {"symbol": "SOL-USDT", "incomeType": "FUNDING_FEE", "income": "1", "time": 1789000000000}]
    assert bingx.parse_funding(income, since_ms=1789370000000) == Decimal("-0.0292")
    assert bingx.parse_fee_rates({"code": 0, "data": {"commission": {"takerCommissionRate": 5e-4, "makerCommissionRate": 2e-4}}}) == (Decimal("0.05"), Decimal("0.02"))
    safe = bingx.parse_permissions({"apiKey": "", "permissions": [2, 3], "ipAddresses": ["1.2.3.4"], "note": ""})
    assert (safe.reading, safe.futures, safe.withdrawals, safe.ip_restricted) == (True, True, False, True)
    risky = bingx.parse_permissions({"code": 0, "data": {"permissions": [1, 2, 3, 5], "ipAddresses": []}})
    assert (risky.withdrawals, risky.ip_restricted) == (True, False)
    assert bingx.parse_permissions({"code": 0, "data": {}}).withdrawals is None
    assert bingx.parse_one_way({"code": 0, "data": {"dualSidePosition": "false"}}) is True


@pytest.mark.parametrize(
    ("status", "executed", "outcome"),
    [
        ("FILLED", "300", OrderOutcome.FILLED),
        ("CANCELLED", "120", OrderOutcome.PARTIAL),
        ("FAILED", "0", OrderOutcome.REJECTED),
        ("PENDING", "0", OrderOutcome.UNKNOWN),
        ("PARTIALLY_FILLED", "100", OrderOutcome.UNKNOWN),
    ],
)
def test_bingx_order_outcomes(status, executed, outcome):
    pepe = bingx_instruments()["1000PEPE-USDT"]
    raw = {"symbol": "1000PEPE-USDT", "orderId": 1736012449498123456, "side": "BUY", "positionSide": "BOTH", "type": "MARKET",
           "origQty": "300", "executedQty": executed, "avgPrice": "0.0034412", "status": status, "clientOrderId": "lkabcol0"}
    report = bingx.parse_order(raw, pepe, "lkabcol0", Decimal("300000"), 1, 2)
    assert report.outcome is outcome
    assert report.filled_tokens == Decimal(executed) * 1000
    assert report.exchange_order_id == "1736012449498123456"


def test_bingx_fills_get_stable_ids_and_the_right_unit():
    pepe = bingx_instruments()["1000PEPE-USDT"]
    rows = [
        {"filledTm": "2026-09-14T08:40:01.000Z", "volume": "100", "price": "0.0034413", "commission": "-0.00017", "currency": "USDT", "orderId": "7"},
        {"filledTm": "2026-09-14T08:40:00.000Z", "volume": "200", "price": "0.0034412", "commission": "-0.00034", "currency": "USDT", "orderId": "7"},
    ]
    fills = bingx.parse_fills(rows, pepe, expected_units=Decimal("300"))
    assert [fill.exchange_fill_id for fill in fills] == ["7-1789375200000-0", "7-1789375201000-1"]
    assert [fill.qty_tokens for fill in fills] == [Decimal("200000"), Decimal("100000")]
    assert fills[0].price == Decimal("0.0000034412") and fills[0].fee == Decimal("0.00034")
    # Volumes that add up to the filled quantity in tokens are taken as tokens.
    in_tokens = [{**row, "volume": str(int(row["volume"]) * 1000)} for row in rows]
    assert sum(fill.qty_tokens for fill in bingx.parse_fills(in_tokens, pepe, expected_units=Decimal("300"))) == Decimal("300000")


async def test_bingx_market_order_submits_one_way_and_reads_the_status():
    adapter = bingx.BingxAdapter("bingx-key-1234567890", "bingx-secret-1234567890")
    await adapter.close()
    doge = bingx_instruments()["DOGE-USDT"]
    status = {"code": 0, "data": {"order": {"symbol": "DOGE-USDT", "orderId": 55, "status": "FILLED", "executedQty": "30", "avgPrice": "0.08415"}}}
    adapter._client = Recorder({
        "swap_v2_private_post_trade_order": {"code": 0, "data": {"order": {"symbol": "DOGE-USDT", "orderId": 55, "clientOrderID": "lkabcos0"}}},
        "swap_v2_private_get_trade_order": status,
    })
    report = await adapter.place_market_order(doge, LegSide.SHORT, True, Decimal("30"), "lkabcos0", 3, True)
    (submit, params), (query, query_params) = adapter._client.calls[:2]
    assert params == {"symbol": "DOGE-USDT", "side": "SELL", "positionSide": "BOTH", "type": "MARKET", "quantity": "30", "clientOrderID": "lkabcos0"}
    assert query_params == {"symbol": "DOGE-USDT", "clientOrderId": "lkabcos0"}
    assert report.outcome is OrderOutcome.FILLED and report.exchange_order_id == "55" and report.filled_tokens == 30


async def test_bingx_missing_order_reads_as_not_found():
    adapter = bingx.BingxAdapter()
    await adapter.close()
    import ccxt

    adapter._client = Recorder({"swap_v2_private_get_trade_order": ccxt.OrderNotFound('bingx {"code":80016,"msg":"order not exist"}')})
    report = await adapter.fetch_order(bingx_instruments()["DOGE-USDT"], "lkabcos0", Decimal("30"))
    assert (report.outcome, report.status) == (OrderOutcome.REJECTED, "not_found")


async def test_bingx_requests_do_not_carry_a_broker_tag():
    adapter = bingx.BingxAdapter("bingx-key-1234567890", "bingx-secret-1234567890")
    try:
        signed = adapter._client.sign("trade/order", ["swap", "v2", "private"], "POST", {"symbol": "DOGE-USDT"})
        assert signed["headers"] == {"X-BX-APIKEY": "bingx-key-1234567890"}
        assert "signature=" in signed["url"]
    finally:
        await adapter.close()


def test_bingx_depth_frames_are_gzip_and_pings_get_an_answer():
    state = MarketState(lambda: 1.0)
    state.set_instruments([bingx_instruments()["1000PEPE-USDT"]])
    frame = {"code": 0, "dataType": "1000PEPE-USDT@depth20@100ms", "ts": 1789376441266,
             "data": {"bids": [["0.0034403", "51656"], ["0.0034399", "323089"]], "asks": [["0.0034406", "2059"], ["0.0034411", "0"]]}}
    assert bingx_market.handle_frame(state, gzip.compress(orjson.dumps(frame))) is None
    book = state.books[("bingx", "1000PEPE-USDT")]
    assert book.asks[0].qty_tokens == Decimal("2059000") and len(book.asks) == 1
    assert book.bids[0].price == Decimal("0.0000034403") and book.exchange_ts_ms == 1789376441266
    assert bingx_market.handle_frame(state, gzip.compress(b"Ping")) == "Pong"
    assert [orjson.loads(m) for m in bingx_market.depth_messages(["BTC-USDT"], True)] == [
        {"id": "s:BTC-USDT@depth20@100ms", "reqType": "sub", "dataType": "BTC-USDT@depth20@100ms"}
    ]


def test_bingx_ticker_rows_refuse_error_answers():
    assert bingx_market.ticker_rows({"code": 0, "data": [{"symbol": "BTC-USDT", "bidPrice": "1", "askPrice": "2", "quoteVolume": "3"}]}) == [
        ("BTC-USDT", 1.0, 2.0, 3.0)
    ]
    with pytest.raises(RuntimeError):
        bingx_market.ticker_rows({"code": 100410, "msg": "busy"})


def test_trade_links_for_new_exchanges():
    assert trade_url(aster_instruments()["1000PEPEUSDT"]) == "https://www.asterdex.com/en/trade/pro/futures/1000PEPEUSDT"
    assert trade_url(bingx_instruments()["1000PEPE-USDT"]) == "https://bingx.com/en/perpetual/1000PEPE-USDT/"
