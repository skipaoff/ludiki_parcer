"""
VFP: Once a minute, stores best prices, mark, index and 24h turnover of every contract in the catalog into `radar_snapshots` for future backtests.
Changes when: the radar snapshot contents or cadence change.
Anti-goal:
1. Storing contracts without a catalog id — snapshots must join to `instruments`.
2. Stale prices dressed as current — a price older than two minutes is stored as missing.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any, Callable, Protocol

from app.instruments.service import PairRecord
from app.market.state import MarketState

INTERVAL_S = 60
MAX_AGE_MS = 120_000


class Catalog(Protocol):
    def records(self) -> list[PairRecord]: ...


def snapshot_rows(records: list[PairRecord], state: MarketState, now_ms: float) -> list[dict[str, Any]]:
    rows = []
    seen: set[int] = set()
    ts = datetime.fromtimestamp(now_ms / 1000, UTC)
    for record in records:
        legs = (
            (record.assessment.a, record.instrument_a_id, record.assessment.volume24h_a_usd),
            (record.assessment.b, record.instrument_b_id, record.assessment.volume24h_b_usd),
        )
        for instrument, instrument_id, catalog_volume in legs:
            if instrument_id is None or instrument_id in seen:
                continue
            seen.add(instrument_id)
            key = (instrument.exchange, instrument.symbol_raw)
            top = state.tops.get(key)
            mark = state.marks.get(key)
            fresh_top = top is not None and now_ms - top.received_ms <= MAX_AGE_MS
            fresh_mark = mark is not None and now_ms - mark.received_ms <= MAX_AGE_MS
            volume = mark.volume24h_usd if fresh_mark and mark.volume24h_usd is not None else catalog_volume
            rows.append(
                {
                    "ts": ts,
                    "instrument_id": instrument_id,
                    "bid": top.bid if fresh_top else None,
                    "ask": top.ask if fresh_top else None,
                    "mark": mark.mark if fresh_mark else None,
                    "index_price": mark.index if fresh_mark else None,
                    "volume24h_usd": volume,
                }
            )
    return rows


class RadarRecorder:
    def __init__(
        self,
        catalog: Catalog,
        state: MarketState,
        submit: Callable[[str, dict[str, Any]], None],
        clock_ms: Callable[[], float] = lambda: time.time() * 1000,
    ) -> None:
        self._catalog = catalog
        self._state = state
        self._submit = submit
        self._clock_ms = clock_ms
        self.snapshots = 0

    async def run(self) -> None:
        while True:
            # Align to the minute so snapshots of different days line up.
            await asyncio.sleep(INTERVAL_S - (time.time() % INTERVAL_S))
            self.record()

    def record(self) -> int:
        rows = snapshot_rows(self._catalog.records(), self._state, self._clock_ms())
        for row in rows:
            self._submit("radar_snapshots", row)
        if rows:
            self.snapshots += 1
        return len(rows)
