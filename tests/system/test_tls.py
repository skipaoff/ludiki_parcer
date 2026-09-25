"""Every exchange call must verify against the OS trust store, not against whatever a library cached on import."""

import ccxt.async_support as ccxt

from app.system.tls import client_session, shared_context, with_shared_context


async def test_exchange_clients_share_one_verifying_context():
    first = with_shared_context(ccxt.binanceusdm())
    second = with_shared_context(ccxt.gate())
    try:
        assert first.ssl_context is second.ssl_context is shared_context()
        assert shared_context().check_hostname
    finally:
        await first.close()
        await second.close()


async def test_a_session_carries_the_shared_context_instead_of_aiohttps_default():
    """On macOS aiohttp's import-time context has no roots at all, so every market call must bring its own."""
    session = client_session()
    try:
        # aiohttp keeps the connector's verification setting in _ssl; there is no public reader for it.
        assert session.connector._ssl is shared_context()
    finally:
        await session.close()


async def test_the_shared_context_is_built_once_for_the_whole_process():
    assert shared_context() is shared_context()
