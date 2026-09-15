"""Funding answers of all six exchanges recorded 15.09.2026 (trimmed)."""

import json
from decimal import Decimal
from pathlib import Path

from app.core.funding import FundingRate
from app.market import funding

FIXTURES = Path(__file__).parent / "fixtures"
EXCHANGE_FIXTURES = Path(__file__).parents[1] / "exchanges" / "fixtures"


def load(name, folder=FIXTURES):
    return json.loads((folder / f"{name}.json").read_text(encoding="utf-8"))


def test_binance_like_intervals_come_from_funding_info():
    rates = funding.parse_binance_like(load("funding_binance_premium"), load("funding_binance_info"))
    btc = next(item for item in load("funding_binance_premium") if item["symbol"] == "BTCUSDT")
    assert rates["BTCUSDT"] == FundingRate(Decimal(btc["lastFundingRate"]) * 100, Decimal(8), btc["nextFundingTime"])
    assert (rates["LPTUSDT"].interval_hours, rates["LSKUSDT"].interval_hours) == (4, 1)
    # A contract missing from fundingInfo settles every 8 hours.
    assert funding.parse_binance_like([{**btc, "symbol": "NEWUSDT"}], [])["NEWUSDT"].interval_hours == 8


def test_mexc_bingx_gate_and_variational_answers():
    mexc = funding.parse_mexc(load("funding_mexc"))
    assert mexc["BTC_USDT"].interval_hours == 8 and mexc["LSK_USDT"].interval_hours == 1
    assert mexc["BTC_USDT"].next_ms == 1789488000000

    bingx = funding.parse_bingx(load("funding_bingx"))
    assert bingx["BTC-USDT"].rate_pct == Decimal("0.00008900") * 100 and bingx["IOST-USDT"].interval_hours == 1

    schedule = funding.parse_gate_schedule(load("funding_gate_contracts"))
    assert schedule["BTC_USDT"] == (28800, 1789488000000) and schedule["2Z_USDT"][0] == 14400
    tickers = load("gate_tickers", EXCHANGE_FIXTURES)
    gate = funding.parse_gate(tickers, schedule)
    btc = next(item for item in tickers if item["contract"] == "BTC_USDT")
    assert gate["BTC_USDT"] == FundingRate(Decimal(btc["funding_rate"]) * 100, Decimal(8), 1789488000000)

    variational = funding.parse_variational(load("funding_variational"))
    # 9.3954 % a year over 8-hour intervals: 9.3954 × 8 / 8760 % per interval, no schedule.
    assert variational["BTC"] == FundingRate(Decimal("0.093954") * 8 / (365 * 24) * 100, Decimal(8), None)
    assert variational["XAUS"].interval_hours == 1  # interval 0: accrues hourly


def test_absurd_rates_are_dropped_and_old_rates_are_not_served():
    rates = funding.parse_bingx({"code": 0, "data": [{"symbol": "X-USDT", "lastFundingRate": "0.5", "fundingIntervalHours": 1}]})
    assert rates == {}  # 50 % an hour
    clock = [1000.0]
    service = funding.FundingService(["bingx"], clock=lambda: clock[0])
    service.store("bingx", {"BTC-USDT": FundingRate(Decimal("0.01"), Decimal(8), None)})
    assert service.rate("bingx", "BTC-USDT") is not None
    clock[0] += funding.STALE_AFTER_S + 1
    assert service.rate("bingx", "BTC-USDT") is None
    assert service.stats()["bingx"]["contracts"] == 1
