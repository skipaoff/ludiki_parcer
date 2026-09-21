"""Bybit, Bitget, KuCoin Futures and Hyperliquid on public answers recorded 15.09.2026; private shapes follow the documentation and must be confirmed by the trial trade."""

import json
from decimal import Decimal
from pathlib import Path

import orjson
import pytest

from app.core.legs import OrderOutcome
from app.core.links import trade_url
from app.core.schemas import LegSide
from app.exchanges.bitget import adapter as bitget
from app.exchanges.bybit import adapter as bybit
from app.exchanges.hyperliquid import adapter as hyperliquid
from app.exchanges.kucoin import adapter as kucoin
from app.market import bitget_market, bybit_market, funding, hyperliquid_market, kucoin_market
from app.market.depth_pool import BookBuffer
from app.market.state import MarketState
from tests.exchanges.test_aster_bingx import Recorder

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def by_symbol(instruments):
    return {item.symbol_raw: item for item in instruments}


def bybit_instruments():
    return by_symbol(bybit.parse_instruments(load("bybit_instruments")["result"]["list"]))


def bitget_instruments():
    return by_symbol(bitget.parse_instruments(load("bitget_contracts")["data"]))


def kucoin_instruments():
    return by_symbol(kucoin.parse_instruments(load("kucoin_contracts")["data"]))


def hyperliquid_instruments():
    return by_symbol(hyperliquid.parse_instruments(load("hyperliquid_meta")))


# ── contracts and quotes ─────────────────────────────────────────────────────


def test_bybit_linear_usdt_perpetuals_only():
    items = bybit_instruments()
    assert set(items) == {"BTCUSDT", "DOGEUSDT", "1000PEPEUSDT"}  # USDC perpetual and dated futures left out
    btc, pepe = items["BTCUSDT"], items["1000PEPEUSDT"]
    assert (btc.qty_step_units, btc.max_market_qty_units, btc.min_notional_usd) == (Decimal("0.001"), Decimal("150.000"), Decimal("5"))
    assert (pepe.token, pepe.qty_unit_tokens, pepe.qty_step_units) == ("PEPE", 1000, 100)
    quote = bybit.parse_quotes(load("bybit_tickers")["result"]["list"])["BTCUSDT"]
    assert quote.bid is not None and quote.index is not None and quote.volume24h_usd is not None


def test_bitget_steps_ticks_and_million_prefix():
    items = bitget_instruments()
    assert (items["BTCUSDT"].qty_step_units, items["BTCUSDT"].price_tick, items["BTCUSDT"].max_market_qty_units) == (Decimal("0.0001"), Decimal("0.1"), 220)
    assert (items["1MBABYDOGEUSDT"].token, items["1MBABYDOGEUSDT"].qty_unit_tokens) == ("BABYDOGE", 1_000_000)
    assert items["1000BONKUSDT"].token == "BONK"
    quote = bitget.parse_quotes(load("bitget_tickers")["data"])["BTCUSDT"]
    assert (quote.bid, quote.ask) == (Decimal("76035.2"), Decimal("76035.3"))


def test_kucoin_lots_multiplier_and_xbt():
    items = kucoin_instruments()
    assert set(items) == {"XBTUSDTM", "DOGEUSDTM", "10000CATUSDTM"}  # USDC-margined and inverse contracts left out
    btc, cat = items["XBTUSDTM"], items["10000CATUSDTM"]
    assert (btc.token, btc.qty_unit_tokens, btc.qty_step_units, btc.price_unit_tokens) == ("BTC", Decimal("0.001"), 1, 1)
    assert (cat.token, cat.qty_unit_tokens, cat.price_unit_tokens) == ("CAT", Decimal("100000"), 10000)  # 10 lots of 10000 CAT
    quote = kucoin.parse_quotes(load("kucoin_alltickers")["data"], load("kucoin_contracts")["data"])["XBTUSDTM"]
    assert quote.bid is not None and quote.mark is not None and quote.volume24h_usd is not None


def test_hyperliquid_coins_skip_delisted_and_read_k_prefix():
    items = hyperliquid_instruments()
    assert set(items) == {"BTC", "DOGE", "kPEPE"}  # MATIC is delisted
    assert (items["BTC"].qty_step_units, items["BTC"].min_notional_usd, items["BTC"].price_tick) == (Decimal("0.00001"), 10, None)
    assert (items["kPEPE"].token, items["kPEPE"].qty_unit_tokens) == ("PEPE", 1000)
    quote = hyperliquid.parse_quotes(load("hyperliquid_meta"))["BTC"]
    assert quote.bid is not None and quote.index is not None


def test_trade_links():
    assert trade_url(bybit_instruments()["BTCUSDT"]) == "https://www.bybit.com/trade/usdt/BTCUSDT"
    assert trade_url(bitget_instruments()["BTCUSDT"]) == "https://www.bitget.com/futures/usdt/BTCUSDT"
    assert trade_url(kucoin_instruments()["XBTUSDTM"]) == "https://www.kucoin.com/trade/futures/XBTUSDTM"
    assert trade_url(hyperliquid_instruments()["kPEPE"]) == "https://app.hyperliquid.xyz/trade/kPEPE"


# ── account answers ──────────────────────────────────────────────────────────


def test_bybit_account_positions_permissions_and_fills():
    pepe = bybit_instruments()["1000PEPEUSDT"]
    positions = bybit.parse_positions(
        [{"symbol": "1000PEPEUSDT", "side": "Sell", "size": "300", "avgPrice": "0.003384", "markPrice": "0.003390", "liqPrice": "",
          "leverage": "3", "tradeMode": 1, "positionIdx": 0, "updatedTime": "1789485200000"},
         {"symbol": "1000PEPEUSDT", "side": "", "size": "0"}],
        {"1000PEPEUSDT": pepe},
    )
    assert len(positions) == 1 and positions[0].side is LegSide.SHORT and positions[0].qty_tokens == 300_000
    assert positions[0].liquidation_price is None and positions[0].margin_mode == "isolated"
    wallet = {"list": [{"totalEquity": "1010.5", "totalAvailableBalance": "900", "totalInitialMargin": "110.5",
                        "coin": [{"coin": "USDT", "walletBalance": "1000"}]}]}
    assert bybit.parse_usdt_balance(wallet) == (Decimal("1000"), Decimal("900"))
    assert bybit.parse_balance(wallet).margin_used_usd == Decimal("110.5")
    safe = bybit.parse_permissions({"readOnly": 0, "permissions": {"ContractTrade": ["Order", "Position"], "Wallet": ["AccountTransfer"]}, "ips": ["1.2.3.4"]})
    assert (safe.futures, safe.withdrawals, safe.ip_restricted) == (True, False, True)
    risky = bybit.parse_permissions({"readOnly": 0, "permissions": {"Derivatives": ["DerivativesTrade"], "Wallet": ["Withdraw"]}, "ips": ["*"]})
    assert (risky.withdrawals, risky.ip_restricted) == (True, False)
    assert bybit.parse_one_way({"list": []}) is None and bybit.parse_one_way({"list": [{"positionIdx": 1}]}) is False
    log = [{"type": "SETTLEMENT", "symbol": "1000PEPEUSDT", "funding": "0.0123", "transactionTime": "1789485300000"},
           {"type": "TRADE", "symbol": "1000PEPEUSDT", "funding": "5", "transactionTime": "1789485300000"}]
    assert bybit.parse_funding(log, "1000PEPEUSDT", 1789485000000) == Decimal("0.0123")
    fills = bybit.parse_fills([{"execId": "e1", "execPrice": "0.003384", "execQty": "300", "execFee": "0.0558", "execTime": "1789485200001", "isMaker": False}], pepe)
    assert (fills[0].qty_tokens, fills[0].price, fills[0].is_maker) == (300_000, Decimal("0.000003384"), False)


@pytest.mark.parametrize(
    ("status", "filled", "outcome"),
    [("Filled", "300", OrderOutcome.FILLED), ("PartiallyFilledCanceled", "100", OrderOutcome.PARTIAL), ("Rejected", "0", OrderOutcome.REJECTED), ("New", "0", OrderOutcome.UNKNOWN)],
)
def test_bybit_order_outcomes(status, filled, outcome):
    pepe = bybit_instruments()["1000PEPEUSDT"]
    report = bybit.parse_order({"orderId": "o1", "orderStatus": status, "cumExecQty": filled, "avgPrice": "0.003384"}, pepe, "lkabcos0", Decimal("300000"), 1, 2)
    assert report.outcome is outcome and report.filled_tokens == Decimal(filled) * 1000


async def test_bybit_order_submits_and_reads_history_when_not_open():
    adapter = bybit.BybitAdapter("bybit-key-123456", "bybit-secret-123456")
    await adapter.close()
    doge = bybit_instruments()["DOGEUSDT"]
    adapter._client = Recorder({
        "private_post_v5_order_create": {"retCode": 0, "result": {"orderId": "o9", "orderLinkId": "lkabcol0"}},
        "private_get_v5_order_realtime": {"retCode": 0, "result": {"list": []}},
        "private_get_v5_order_history": {"retCode": 0, "result": {"list": [{"orderId": "o9", "orderStatus": "Filled", "cumExecQty": "60", "avgPrice": "0.0816"}]}},
    })
    report = await adapter.place_market_order(doge, LegSide.SHORT, False, Decimal("60"), "lkabcol0", 3, True)
    name, params = adapter._client.calls[0]
    assert params == {"category": "linear", "symbol": "DOGEUSDT", "side": "Buy", "orderType": "Market", "qty": "60", "orderLinkId": "lkabcol0",
                      "positionIdx": 0, "reduceOnly": True}
    assert report.outcome is OrderOutcome.FILLED and report.exchange_order_id == "o9"


def test_bitget_account_answers():
    bonk = bitget_instruments()["1000BONKUSDT"]
    positions = bitget.parse_positions(
        [{"symbol": "1000BONKUSDT", "holdSide": "long", "total": "500", "openPriceAvg": "0.0189", "markPrice": "0.019", "liquidationPrice": "0.012",
          "leverage": "3", "marginMode": "crossed", "uTime": "1789485200000"}],
        {"1000BONKUSDT": bonk},
    )
    assert (positions[0].side, positions[0].qty_tokens, positions[0].margin_mode) == (LegSide.LONG, 500_000, "cross")
    account = [{"marginCoin": "USDT", "accountEquity": "1200", "available": "1000", "crossedMargin": "150", "isolatedMargin": "50"}]
    balance = bitget.parse_balance(account)
    assert (balance.equity_usd, balance.available_usd, balance.margin_used_usd) == (Decimal("1200"), Decimal("1000"), Decimal("200"))
    assert bitget.parse_one_way({"posMode": "one_way_mode"}) is True and bitget.parse_one_way({"posMode": "hedge_mode"}) is False
    unknown = bitget.parse_permissions({"authorities": ["trade", "readonly"], "ips": ""})
    assert (unknown.futures, unknown.withdrawals, unknown.ip_restricted) == (True, None, False)
    assert bitget.parse_permissions({"authorities": ["coow", "wtw"], "ips": "1.2.3.4"}).withdrawals is True
    assert bitget.parse_funding([{"businessType": "contract_settle_fee", "symbol": "1000BONKUSDT", "amount": "-0.02", "cTime": "1789485300000"}], "1000BONKUSDT", 0) == Decimal("-0.02")
    report = bitget.parse_order({"orderId": "b1", "state": "filled", "baseVolume": "500", "priceAvg": "0.0189"}, bonk, "lkabcol0", Decimal("500000"), 1, 2)
    assert report.outcome is OrderOutcome.FILLED and report.avg_price == Decimal("0.0000189")
    fills = bitget.parse_fills({"fillList": [{"tradeId": "t1", "price": "0.0189", "baseVolume": "500", "feeDetail": [{"totalFee": "-0.0057", "feeCoin": "USDT"}], "cTime": "1", "tradeScope": "taker"}]}, bonk)
    assert (fills[0].fee, fills[0].is_maker) == (Decimal("0.0057"), False)


async def test_bitget_order_params_and_status():
    adapter = bitget.BitgetAdapter("bitget-key-123456", "bitget-secret-123456", "pass phrase")
    await adapter.close()
    doge = bitget_instruments()["DOGEUSDT"]
    adapter._client = Recorder({
        "private_mix_post_v2_mix_order_place_order": {"code": "00000", "data": {"orderId": "b7", "clientOid": "lkabcol0"}},
        "private_mix_get_v2_mix_order_detail": {"code": "00000", "data": {"orderId": "b7", "state": "filled", "baseVolume": "60", "priceAvg": "0.0816"}},
    })
    report = await adapter.place_market_order(doge, LegSide.LONG, True, Decimal("60"), "lkabcol0", 3, True)
    assert adapter._client.calls[0][1] == {"productType": "USDT-FUTURES", "marginCoin": "USDT", "symbol": "DOGEUSDT", "marginMode": "isolated",
                                           "size": "60", "side": "buy", "orderType": "market", "clientOid": "lkabcol0", "reduceOnly": "NO"}
    assert report.outcome is OrderOutcome.FILLED


def test_kucoin_account_answers():
    cat = kucoin_instruments()["10000CATUSDTM"]
    positions = kucoin.parse_positions(
        [{"symbol": "10000CATUSDTM", "currentQty": -3, "avgEntryPrice": 0.045, "markPrice": 0.046, "liquidationPrice": 0.06, "leverage": 3,
          "marginMode": "ISOLATED", "currentTimestamp": 1789485200000}],
        {"10000CATUSDTM": cat},
    )
    assert (positions[0].side, positions[0].qty_tokens, positions[0].entry_price) == (LegSide.SHORT, Decimal("300000"), Decimal("0.0000045"))
    assert kucoin.parse_one_way({"positionMode": "0"}) is True and kucoin.parse_one_way({"positionMode": 1}) is False
    assert kucoin.parse_one_way({"positionMode": "2"}) is None
    permissions = kucoin.parse_permissions({"permission": "General,Futures", "ipWhitelist": "1.2.3.4"})
    assert (permissions.futures, permissions.withdrawals, permissions.ip_restricted) == (True, False, True)
    assert kucoin.parse_permissions({"permission": "General,Futures,Withdrawal"}).withdrawals is True
    balance = kucoin.parse_balance({"accountEquity": 500, "availableBalance": 400, "positionMargin": 80, "orderMargin": 20})
    assert balance.margin_used_usd == 100
    assert kucoin.parse_funding({"dataList": [{"funding": -0.12, "timePoint": 1789485300000}, {"funding": 1, "timePoint": 1}]}, 1789485000000) == Decimal("-0.12")
    order = {"id": "k1", "symbol": "10000CATUSDTM", "status": "done", "isActive": False, "cancelExist": False, "filledSize": 3, "filledValue": "13.5"}
    report = kucoin.parse_order(order, cat, "lkabcol0", Decimal("300000"), 1, 2)
    assert (report.outcome, report.filled_tokens, report.avg_price) == (OrderOutcome.FILLED, 300_000, Decimal("0.000045"))
    open_order = {**order, "status": "open", "isActive": True, "filledSize": 0}
    assert kucoin.parse_order(open_order, cat, "lkabcol0", Decimal("300000"), 1, 2).outcome is OrderOutcome.UNKNOWN
    fills = kucoin.parse_fills({"items": [{"tradeId": "f1", "price": "0.045", "size": 3, "fee": "0.008", "feeCurrency": "USDT", "liquidity": "taker",
                                           "tradeTime": 1789485200000000000}]}, cat)
    assert (fills[0].qty_tokens, fills[0].ts_ms) == (300_000, 1789485200000)


def test_hyperliquid_account_answers_and_order_statuses():
    pepe = hyperliquid_instruments()["kPEPE"]
    state = {"marginSummary": {"accountValue": "1500.5", "totalMarginUsed": "210"}, "withdrawable": "1200", "time": 1789485200000,
             "assetPositions": [{"position": {"coin": "kPEPE", "szi": "-3000", "entryPx": "0.003386", "positionValue": "10.2",
                                              "liquidationPx": "0.0045", "leverage": {"type": "isolated", "value": 3}}}]}
    positions = hyperliquid.parse_positions(state, {"kPEPE": pepe})
    assert (positions[0].side, positions[0].qty_tokens, positions[0].margin_mode) == (LegSide.SHORT, 3_000_000, "isolated")
    assert hyperliquid.parse_balance(state).available_usd == Decimal("1200")
    assert hyperliquid.parse_funding([{"time": 1789485300000, "delta": {"coin": "kPEPE", "usdc": "-0.004"}},
                                      {"time": 1789485300000, "delta": {"coin": "BTC", "usdc": "1"}}], "kPEPE", 0) == Decimal("-0.004")
    filled = hyperliquid.parse_submit({"filled": {"totalSz": "3000", "avgPx": "0.003386", "oid": 77}}, pepe, "lkabcos0", Decimal("3000000"), 1, 2)
    assert (filled.outcome, filled.exchange_order_id, filled.avg_price) == (OrderOutcome.FILLED, "77", Decimal("0.000003386"))
    error = hyperliquid.parse_submit({"error": "Order could not immediately match against any resting orders."}, pepe, "lkabcos0", Decimal("3000000"), 1, 2)
    assert error.outcome is OrderOutcome.REJECTED
    missing = hyperliquid.parse_order_status({"status": "unknownOid"}, pepe, "lkabcos0", Decimal("3000000"), 1)
    assert (missing.outcome, missing.status) == (OrderOutcome.REJECTED, "not_found")
    done = hyperliquid.parse_order_status({"status": "order", "order": {"order": {"oid": 77, "origSz": "3000", "sz": "0"}, "status": "filled"}},
                                          pepe, "lkabcos0", Decimal("3000000"), 1)
    assert (done.outcome, done.filled_tokens) == (OrderOutcome.FILLED, 3_000_000)
    assert hyperliquid.cloid("lkabcos0").startswith("0x") and len(hyperliquid.cloid("lkabcos0")) == 34
    assert hyperliquid.cloid("lkabcos0") == hyperliquid.cloid("lkabcos0")
    assert hyperliquid.valid_keys("0x" + "ab" * 20, "11" * 32) and not hyperliquid.valid_keys("wallet", "11" * 32)


# ── market feeds ─────────────────────────────────────────────────────────────


def state_with(*instruments):
    state = MarketState(lambda: 1.0)
    state.set_instruments(list(instruments))
    return state


def test_bybit_book_is_built_from_snapshot_and_deltas():
    pepe = bybit_instruments()["1000PEPEUSDT"]
    state = state_with(pepe)
    books, buffer = bybit_market.LocalBooks(), BookBuffer(state, "bybit")
    delta = {"topic": "orderbook.50.1000PEPEUSDT", "type": "delta", "ts": 3, "data": {"s": "1000PEPEUSDT", "b": [["0.0033830", "0"]], "a": []}}
    bybit_market.handle_frame(books, buffer, state, orjson.dumps(delta))
    assert buffer.flush() == 0  # a delta before its snapshot builds nothing
    snapshot = {"topic": "orderbook.50.1000PEPEUSDT", "type": "snapshot", "ts": 1,
                "data": {"s": "1000PEPEUSDT", "b": [["0.0033830", "500"], ["0.0033820", "900"]], "a": [["0.0033840", "700"]]}}
    bybit_market.handle_frame(books, buffer, state, orjson.dumps(snapshot))
    bybit_market.handle_frame(books, buffer, state, orjson.dumps({**delta, "data": {"s": "1000PEPEUSDT", "b": [["0.0033830", "0"]], "a": [["0.0033835", "10"]]}}))
    assert buffer.flush() == 1
    book = state.books[("bybit", "1000PEPEUSDT")]
    assert book.bids[0].price == Decimal("0.000003382") and book.asks[0].price == Decimal("0.0000033835") and book.asks[0].qty_tokens == 10_000
    messages = [orjson.loads(m) for m in bybit_market.depth_messages(["BTCUSDT"], True)]
    assert [m["op"] for m in messages] == ["unsubscribe", "subscribe"] and messages[1]["args"] == ["orderbook.50.BTCUSDT"]


def test_bitget_kucoin_and_hyperliquid_book_frames():
    bonk = bitget_instruments()["1000BONKUSDT"]
    cat = kucoin_instruments()["10000CATUSDTM"]
    pepe = hyperliquid_instruments()["kPEPE"]
    state = state_with(bonk, cat, pepe)
    bitget_market.handle_frame(state, orjson.dumps({"action": "snapshot", "arg": {"instType": "USDT-FUTURES", "channel": "books15", "instId": "1000BONKUSDT"},
                                                    "data": [{"bids": [["0.0189", "40"]], "asks": [["0.019", "30"]], "ts": "5"}]}))
    assert state.books[("bitget", "1000BONKUSDT")].asks[0].qty_tokens == 30_000
    bitget_market.handle_frame(state, "pong")

    buffer = BookBuffer(state, "kucoin")
    kucoin_market.handle_frame(buffer, state, orjson.dumps({"topic": "/contractMarket/level2Depth50:10000CATUSDTM", "type": "message", "subject": "level2",
                                                            "data": {"bids": [["0.045", 2]], "asks": [["0.046", 5]], "ts": 7}}))
    buffer.flush()
    assert state.books[("kucoin", "10000CATUSDTM")].asks[0].qty_tokens == 500_000
    assert orjson.loads(next(iter(kucoin_market.depth_messages(["XBTUSDTM"], True))))["topic"] == "/contractMarket/level2Depth50:XBTUSDTM"

    hyperliquid_market.handle_frame(state, orjson.dumps({"channel": "l2Book", "data": {"coin": "kPEPE", "time": 9, "levels": [
        [{"px": "0.003386", "sz": "4000", "n": 3}], [{"px": "0.003387", "sz": "1000", "n": 1}]]}}))
    book = state.books[("hyperliquid", "kPEPE")]
    assert (book.bids[0].qty_tokens, book.asks[0].price) == (4_000_000, Decimal("0.000003387"))
    subscription = orjson.loads(next(iter(hyperliquid_market.depth_messages(["BTC"], True))))
    assert subscription["subscription"] == {"type": "l2Book", "coin": "BTC", "fast": True}


def test_rest_rows_refuse_error_answers():
    with pytest.raises(RuntimeError):
        bybit_market.ticker_rows({"retCode": 10006, "retMsg": "Too many visits"})
    with pytest.raises(RuntimeError):
        bitget_market.ticker_rows({"code": "40001", "msg": "busy"})
    with pytest.raises(RuntimeError):
        kucoin_market.ticker_rows({"code": "429000"})
    rows = hyperliquid_market.context_rows(load("hyperliquid_meta"))
    assert {row[0] for row in rows} >= {"BTC", "kPEPE"}


# ── funding ──────────────────────────────────────────────────────────────────


def test_funding_of_the_four_exchanges():
    btc = next(item for item in load("bybit_tickers")["result"]["list"] if item["symbol"] == "BTCUSDT")
    rate = funding.parse_bybit(load("bybit_tickers"))["BTCUSDT"]
    assert (rate.rate_pct, rate.interval_hours, rate.next_ms) == (Decimal(btc["fundingRate"]) * 100, Decimal(btc["fundingIntervalHour"]), int(btc["nextFundingTime"]))
    assert funding.parse_bitget(load("bitget_current_fund"))["BTCUSDT"].interval_hours == 8
    kucoin_rate = funding.parse_kucoin(load("kucoin_contracts"))["XBTUSDTM"]
    assert kucoin_rate.interval_hours == 8 and kucoin_rate.next_ms is not None
    hl = funding.parse_hyperliquid(load("hyperliquid_meta"), 1789485206000)
    assert hl["BTC"].interval_hours == 1 and hl["BTC"].next_ms == 1789488000000
    assert "MATIC" not in hl
