"""
VFP: Stores the instrument and pair catalog and the manual pair flags in the `instruments` and `pairs` tables.
Changes when: the catalog tables or the way manual flags are kept change.
Anti-goal:
1. Losing manual decisions on refresh — upserts never touch manually_verified or blacklisted.
2. Deleting delisted contracts — they are marked inactive, trades and episodes keep pointing at them.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from app.core.pairs import PairAssessment
from app.core.schemas import Instrument
from app.storage.bulk import bulk_insert

INSTRUMENT_EXTRA = {"is_active": "TRUE", "delisted_at": "NULL", "updated_at": "now()"}
INSTRUMENT_CONFLICT = """
ON CONFLICT (exchange, symbol_raw) DO UPDATE SET
    token = EXCLUDED.token,
    qty_unit_tokens = EXCLUDED.qty_unit_tokens,
    price_unit_tokens = EXCLUDED.price_unit_tokens,
    qty_step_units = EXCLUDED.qty_step_units,
    min_qty_units = EXCLUDED.min_qty_units,
    max_market_qty_units = EXCLUDED.max_market_qty_units,
    min_notional_usd = EXCLUDED.min_notional_usd,
    price_tick = EXCLUDED.price_tick,
    is_active = TRUE,
    delisted_at = NULL,
    updated_at = now()
"""

DEACTIVATE_MISSING = """
UPDATE instruments SET is_active = FALSE, delisted_at = coalesce(delisted_at, now()), updated_at = now()
WHERE exchange = $1 AND is_active AND NOT (symbol_raw = ANY($2::text[]))
"""

PAIR_EXTRA = {"updated_at": "now()"}
PAIR_CONFLICT = """
ON CONFLICT (instrument_a_id, instrument_b_id) DO UPDATE SET
    token = EXCLUDED.token,
    common_qty_step_tokens = EXCLUDED.common_qty_step_tokens,
    suspicious = EXCLUDED.suspicious,
    suspicious_reason = EXCLUDED.suspicious_reason,
    updated_at = now()
"""


class CatalogWriteError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StoredPair:
    pair_id: int
    manually_verified: bool
    blacklisted: bool


def pair_key(assessment: PairAssessment) -> str:
    return f"{assessment.a.exchange}:{assessment.a.symbol_raw}|{assessment.b.exchange}:{assessment.b.symbol_raw}"


async def save_catalog(
    pool: asyncpg.Pool,
    instruments: list[Instrument],
    assessments: list[PairAssessment],
) -> dict[str, StoredPair]:
    """Upsert everything in one transaction and return the stored pair id and manual flags per pair key."""
    async with pool.acquire() as connection, connection.transaction():
        await bulk_insert(
            connection,
            "instruments",
            [
                {
                    "exchange": item.exchange,
                    "symbol_raw": item.symbol_raw,
                    "token": item.token,
                    "qty_unit_tokens": item.qty_unit_tokens,
                    "price_unit_tokens": item.price_unit_tokens,
                    "qty_step_units": item.qty_step_units,
                    "min_qty_units": item.min_qty_units,
                    "max_market_qty_units": item.max_market_qty_units,
                    "min_notional_usd": item.min_notional_usd,
                    "price_tick": item.price_tick,
                }
                for item in instruments
            ],
            extra=INSTRUMENT_EXTRA,
            tail=INSTRUMENT_CONFLICT,
        )
        for exchange in sorted({item.exchange for item in instruments}):
            active = [item.symbol_raw for item in instruments if item.exchange == exchange]
            await connection.execute(DEACTIVATE_MISSING, exchange, active)

        rows = await connection.fetch("SELECT id, exchange, symbol_raw FROM instruments WHERE is_active")
        ids = {(row["exchange"], row["symbol_raw"]): row["id"] for row in rows}
        missing = [(item.exchange, item.symbol_raw) for item in instruments if (item.exchange, item.symbol_raw) not in ids]
        if missing:
            # Never trust a bulk write blindly: a driver or event-loop fault once dropped rows without an error.
            raise CatalogWriteError(f"{len(missing)} instruments did not reach the database, e.g. {missing[:3]}")

        ordered: dict[str, tuple[int, int]] = {}
        values = []
        for assessment in assessments:
            a_id = ids[(assessment.a.exchange, assessment.a.symbol_raw)]
            b_id = ids[(assessment.b.exchange, assessment.b.symbol_raw)]
            low, high = sorted((a_id, b_id))
            ordered[pair_key(assessment)] = (low, high)
            values.append(
                {
                    "token": assessment.token,
                    "instrument_a_id": low,
                    "instrument_b_id": high,
                    "common_qty_step_tokens": assessment.common_step_tokens,
                    "suspicious": assessment.suspicious_reason is not None,
                    "suspicious_reason": assessment.suspicious_reason,
                }
            )
        await bulk_insert(connection, "pairs", values, extra=PAIR_EXTRA, tail=PAIR_CONFLICT)

        stored = await connection.fetch("SELECT id, instrument_a_id, instrument_b_id, manually_verified, blacklisted FROM pairs")
        by_ids = {(row["instrument_a_id"], row["instrument_b_id"]): row for row in stored}
        return {
            key: StoredPair(by_ids[ids_pair]["id"], by_ids[ids_pair]["manually_verified"], by_ids[ids_pair]["blacklisted"])
            for key, ids_pair in ordered.items()
        }


async def set_pair_flags(
    pool: asyncpg.Pool, pair_id: int, manually_verified: bool | None, blacklisted: bool | None
) -> StoredPair:
    row = await pool.fetchrow(
        """
        UPDATE pairs SET
            manually_verified = coalesce($2, manually_verified),
            blacklisted = coalesce($3, blacklisted),
            updated_at = now()
        WHERE id = $1
        RETURNING id, manually_verified, blacklisted
        """,
        pair_id,
        manually_verified,
        blacklisted,
    )
    if row is None:
        raise KeyError(pair_id)
    return StoredPair(row["id"], row["manually_verified"], row["blacklisted"])
