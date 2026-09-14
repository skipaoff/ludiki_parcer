import asyncio
import hashlib
import hmac

import orjson

from app.market.private_streams import OrderEvents, binance_event, mexc_event, mexc_login_message


def test_binance_user_data_events():
    order = {"e": "ORDER_TRADE_UPDATE", "T": 1, "E": 1, "o": {"s": "SOLUSDT", "c": "lkabcol0", "S": "BUY", "o": "MARKET", "X": "FILLED", "z": "10", "ap": "143.1"}}
    assert binance_event(order) == ("order", "lkabcol0")
    assert binance_event({"e": "ACCOUNT_UPDATE", "a": {"m": "ORDER", "B": [], "P": []}}) == ("account", None)
    assert binance_event({"e": "listenKeyExpired"}) == ("expired", None)


def test_mexc_personal_events():
    assert mexc_event({"channel": "push.personal.order", "data": {"externalOid": "lkabcos0", "state": 3}}) == ("order", "lkabcos0")
    assert mexc_event({"channel": "push.personal.position", "data": {}}) == ("account", None)
    assert mexc_event({"channel": "rs.login", "data": "success"}) == ("login", "success")


def test_mexc_login_signature_is_hmac_of_key_and_time():
    message = orjson.loads(mexc_login_message("mx0key", "secret", 1789300000000))
    expected = hmac.new(b"secret", b"mx0key1789300000000", hashlib.sha256).hexdigest()
    assert message["param"] == {"apiKey": "mx0key", "reqTime": "1789300000000", "signature": expected}


async def test_order_event_wakes_its_waiter_early():
    events = OrderEvents()

    async def later():
        await asyncio.sleep(0.01)
        events.notify("lkabcol0")

    asyncio.get_running_loop().create_task(later())
    assert await events.wait("lkabcol0", timeout_s=5) is True
    assert await events.wait("other", timeout_s=0.01) is False
