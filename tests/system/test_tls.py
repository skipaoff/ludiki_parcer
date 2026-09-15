import ccxt.async_support as ccxt

from app.system.tls import shared_context, with_shared_context


async def test_exchange_clients_share_one_verifying_context():
    first = with_shared_context(ccxt.binanceusdm())
    second = with_shared_context(ccxt.gate())
    try:
        assert first.ssl_context is second.ssl_context is shared_context()
        assert shared_context().check_hostname
    finally:
        await first.close()
        await second.close()
