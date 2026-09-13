"""
VFP: Turns the gap lifecycle into database history — one `opportunity_episodes` row per gap that reached the feed, its once-a-second samples and its final outcome.
Changes when: what is recorded about a gap, or how often, changes (PLAN.md, sections 8 and 11).
Anti-goal:
1. Waiting for the database — rows go to the write queue with locally issued ids.
2. Recording flicker — candidates that never reached the feed are counted, not stored.
3. Losing an open episode on a crash — the row is refreshed every minute and dangling rows are closed at the next start.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Callable, Protocol

from app.core.episodes import EpisodeState
from app.instruments.service import PairRecord
from app.storage.ids import LocalIds

SAMPLE_EVERY_MS = 1_000
REFRESH_EVERY_MS = 60_000


class QuoteLike(Protocol):
    long_avg: Decimal | None
    short_avg: Decimal | None
    long_exit_avg: Decimal | None
    short_exit_avg: Decimal | None
    roi_gross_pct: Decimal | None
    roi_net_pct: Decimal | None
    exit_spread_pct: Decimal | None
    capacity_usd: Decimal | None
    age_long_ms: float | None
    age_short_ms: float | None


class EpisodeSink(Protocol):
    def entered(self, episode_key: str, record: PairRecord, state: EpisodeState, now_ms: int) -> None: ...

    def sampled(self, episode_key: str, record: PairRecord, state: EpisodeState, quote: QuoteLike | None, now_ms: int) -> None: ...

    def left_feed(self, episode_key: str, now_ms: int) -> None: ...

    def ended(self, episode_key: str, record: PairRecord, state: EpisodeState, now_ms: int) -> None: ...


class NullSink:
    def entered(self, *args: Any) -> None:
        pass

    def sampled(self, *args: Any) -> None:
        pass

    def left_feed(self, *args: Any) -> None:
        pass

    def ended(self, *args: Any) -> None:
        pass


def _ts(ms: int | None) -> datetime | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, UTC)


@dataclass
class _Open:
    id: int
    long_exchange: str
    short_exchange: str
    settings: dict[str, Any]
    left_feed_ms: int | None = None
    samples: int = 0
    last_sample_ms: int = 0
    last_refresh_ms: int = 0
    capacity_peak: Decimal | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class EpisodeRecorder:
    def __init__(
        self,
        submit: Callable[[str, dict[str, Any]], None],
        settings_snapshot: Callable[[], dict[str, Any]],
        ids: LocalIds | None = None,
    ) -> None:
        self._submit = submit
        self._settings_snapshot = settings_snapshot
        self._ids = ids or LocalIds()
        self._open: dict[str, _Open] = {}
        self.recorded = 0

    @property
    def open_count(self) -> int:
        return len(self._open)

    def episode_id(self, episode_key: str) -> int | None:
        opened = self._open.get(episode_key)
        return None if opened is None else opened.id

    def entered(self, episode_key: str, record: PairRecord, state: EpisodeState, now_ms: int) -> None:
        if episode_key in self._open:
            return
        long_exchange = episode_key.rsplit(">", 1)[1]
        short_exchange = record.assessment.b.exchange if long_exchange == record.assessment.a.exchange else record.assessment.a.exchange
        opened = _Open(self._ids.next(), long_exchange, short_exchange, self._settings_snapshot())
        self._open[episode_key] = opened
        self.recorded += 1
        self._write(opened, record, state, now_ms, ended_ms=None, reason=None)

    def sampled(self, episode_key: str, record: PairRecord, state: EpisodeState, quote: QuoteLike | None, now_ms: int) -> None:
        opened = self._open.get(episode_key)
        if opened is None or quote is None or now_ms - opened.last_sample_ms < SAMPLE_EVERY_MS:
            return
        opened.last_sample_ms = now_ms
        opened.samples += 1
        if quote.capacity_usd is not None and (opened.capacity_peak is None or quote.capacity_usd > opened.capacity_peak):
            opened.capacity_peak = quote.capacity_usd
        self._submit(
            "episode_samples",
            {
                "ts": _ts(now_ms),
                "episode_id": opened.id,
                "ask_long_vwap": quote.long_avg,
                "bid_short_vwap": quote.short_avg,
                "roi_gross_pct": quote.roi_gross_pct,
                "roi_net_pct": quote.roi_net_pct,
                "capacity_usd": quote.capacity_usd,
                "bid_long_exit_vwap": quote.long_exit_avg,
                "ask_short_exit_vwap": quote.short_exit_avg,
                "exit_spread_pct": quote.exit_spread_pct,
                "age_long_ms": None if quote.age_long_ms is None else int(quote.age_long_ms),
                "age_short_ms": None if quote.age_short_ms is None else int(quote.age_short_ms),
            },
        )
        if now_ms - opened.last_refresh_ms >= REFRESH_EVERY_MS:
            self._write(opened, record, state, now_ms, ended_ms=None, reason=None)

    def left_feed(self, episode_key: str, now_ms: int) -> None:
        opened = self._open.get(episode_key)
        if opened is not None and opened.left_feed_ms is None:
            opened.left_feed_ms = now_ms

    def ended(self, episode_key: str, record: PairRecord, state: EpisodeState, now_ms: int) -> None:
        opened = self._open.pop(episode_key, None)
        if opened is None:
            return
        self._write(opened, record, state, now_ms, ended_ms=now_ms, reason=state.end_reason)

    def _write(
        self, opened: _Open, record: PairRecord, state: EpisodeState, now_ms: int, ended_ms: int | None, reason: str | None
    ) -> None:
        opened.last_refresh_ms = now_ms
        assessment = record.assessment
        volume_long, volume_short = (
            (assessment.volume24h_a_usd, assessment.volume24h_b_usd)
            if opened.long_exchange == assessment.a.exchange
            else (assessment.volume24h_b_usd, assessment.volume24h_a_usd)
        )
        self._submit(
            "opportunity_episodes",
            {
                "id": opened.id,
                "strategy": "price_gap",
                "pair_id": record.pair_id,
                "token": assessment.token,
                "long_exchange": opened.long_exchange,
                "short_exchange": opened.short_exchange,
                "size_usd": opened.settings.get("size_usd"),
                "detected_at": _ts(state.detected_ms),
                "entered_feed_at": _ts(state.first_entered_feed_ms),
                "left_feed_at": _ts(opened.left_feed_ms),
                "converged_at": _ts(ended_ms) if reason == "converged" else None,
                "ended_at": _ts(ended_ms),
                "end_reason": reason,
                "roi_first": state.roi_at_feed_entry,
                "roi_peak": state.roi_peak,
                "roi_peak_at": _ts(state.roi_peak_ms),
                "capacity_peak_usd": opened.capacity_peak,
                "volume24h_long_usd": volume_long,
                "volume24h_short_usd": volume_short,
                "index_diff_pct": assessment.index_gap_pct,
                "suspicious": record.suspicious,
                "opened": False,
                "trade_id": None,
                "missed_pnl_best_pct": state.missed_pnl_best_pct,
                "missed_best_exit_at": _ts(state.missed_best_exit_ms),
                "samples_count": opened.samples,
                "settings_snapshot": opened.settings,
            },
        )
