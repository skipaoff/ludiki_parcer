"""
VFP: The next lifecycle state of one gap episode, plus the events that transition produced.
Changes when: the rules for when a gap appears in the feed, leaves it, or counts as converged change.
Anti-goal:
1. Reading the clock — time arrives inside each sample, so replays of recorded data give identical results.
2. Writing to the database — the recorder consumes the returned events; this module only decides.

Lifecycle (docs/PLAN.md, section 8):
    candidate -> in_feed -> tracking -> ended
                  ^            |
                  +------------+   (the same episode may re-enter the feed)
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum


class Phase(str, Enum):
    CANDIDATE = "candidate"
    IN_FEED = "in_feed"
    TRACKING = "tracking"
    ENDED = "ended"


class EventKind(str, Enum):
    ENTERED_FEED = "entered_feed"
    LEFT_FEED = "left_feed"
    ENDED = "ended"


@dataclass(frozen=True, slots=True)
class EpisodeRules:
    """
    enter_after_ms — how long a gap must live before it is shown: dips below the exit level shorter than
    exit_after_ms do not reset that clock, a longer dip (or no measurement) does. 45 s by the owner's rule (14.09.2026):
    one-second lead-lag blips cannot be caught by hand.
    """

    min_roi_net_pct: Decimal
    enter_after_ms: int = 45_000
    exit_hysteresis_pct: Decimal = Decimal("0.10")
    exit_after_ms: int = 2_000
    max_lifetime_ms: int = 24 * 3600 * 1000


@dataclass(frozen=True, slots=True)
class Sample:
    """
    One observation of a pair. roi_net_pct is None when the book is too thin for the configured size.
    exit_spread_pct is the cost of closing the same size right now.
    """

    ts_ms: int
    roi_net_pct: Decimal | None
    exit_spread_pct: Decimal | None


@dataclass(frozen=True, slots=True)
class EpisodeState:
    phase: Phase
    detected_ms: int
    above_since_ms: int | None = None
    below_since_ms: int | None = None
    first_entered_feed_ms: int | None = None
    roi_at_feed_entry: Decimal | None = None
    roi_peak: Decimal | None = None
    roi_peak_ms: int | None = None
    missed_pnl_best_pct: Decimal | None = None
    missed_best_exit_ms: int | None = None
    end_reason: str | None = None


@dataclass(frozen=True, slots=True)
class EpisodeEvent:
    kind: EventKind
    ts_ms: int
    reason: str | None = None


def _above(sample: Sample, threshold: Decimal) -> bool:
    return sample.roi_net_pct is not None and sample.roi_net_pct >= threshold


def _track_peak(state: EpisodeState, sample: Sample) -> EpisodeState:
    if sample.roi_net_pct is None or (state.roi_peak is not None and sample.roi_net_pct <= state.roi_peak):
        return state
    return replace(state, roi_peak=sample.roi_net_pct, roi_peak_ms=sample.ts_ms)


def _track_missed_pnl(state: EpisodeState, sample: Sample) -> EpisodeState:
    """Hypothetical result of entering at the first feed entry and exiting at this sample."""
    if state.roi_at_feed_entry is None or sample.exit_spread_pct is None:
        return state
    pnl = state.roi_at_feed_entry - sample.exit_spread_pct
    if state.missed_pnl_best_pct is not None and pnl <= state.missed_pnl_best_pct:
        return state
    return replace(state, missed_pnl_best_pct=pnl, missed_best_exit_ms=sample.ts_ms)


def _enter_feed(state: EpisodeState, sample: Sample) -> EpisodeState:
    first_entry = state.first_entered_feed_ms is None
    return replace(
        state,
        phase=Phase.IN_FEED,
        below_since_ms=None,
        first_entered_feed_ms=sample.ts_ms if first_entry else state.first_entered_feed_ms,
        roi_at_feed_entry=sample.roi_net_pct if first_entry else state.roi_at_feed_entry,
    )


def step(
    state: EpisodeState | None,
    sample: Sample,
    rules: EpisodeRules,
) -> tuple[EpisodeState | None, list[EpisodeEvent]]:
    """Advance one episode by one sample. None state means no episode exists for the pair yet."""
    if state is not None and state.phase is Phase.ENDED:
        return state, []

    is_above = _above(sample, rules.min_roi_net_pct)

    if state is None:
        if not is_above:
            return None, []
        state = EpisodeState(phase=Phase.CANDIDATE, detected_ms=sample.ts_ms, above_since_ms=sample.ts_ms)

    if sample.ts_ms - state.detected_ms >= rules.max_lifetime_ms and state.phase is not Phase.CANDIDATE:
        return end(state, sample.ts_ms, "timeout")

    if state.phase is Phase.CANDIDATE:
        if not _above(sample, rules.min_roi_net_pct - rules.exit_hysteresis_pct):
            below_since = state.below_since_ms if state.below_since_ms is not None else sample.ts_ms
            if sample.ts_ms - below_since >= rules.exit_after_ms:
                return None, []
            return replace(state, below_since_ms=below_since), []
        state = replace(state, below_since_ms=None)
        if is_above and sample.ts_ms - state.above_since_ms >= rules.enter_after_ms:
            entered = _track_peak(_enter_feed(state, sample), sample)
            return entered, [EpisodeEvent(EventKind.ENTERED_FEED, sample.ts_ms)]
        return state, []

    state = _track_missed_pnl(_track_peak(state, sample), sample)

    if state.phase is Phase.IN_FEED:
        stays = _above(sample, rules.min_roi_net_pct - rules.exit_hysteresis_pct)
        if stays:
            return replace(state, below_since_ms=None), []
        below_since = state.below_since_ms if state.below_since_ms is not None else sample.ts_ms
        if sample.ts_ms - below_since >= rules.exit_after_ms:
            left = replace(state, phase=Phase.TRACKING, below_since_ms=None, above_since_ms=None)
            return left, [EpisodeEvent(EventKind.LEFT_FEED, sample.ts_ms)]
        return replace(state, below_since_ms=below_since), []

    # Phase.TRACKING
    if sample.exit_spread_pct is not None and sample.exit_spread_pct <= 0:
        return end(state, sample.ts_ms, "converged")
    if not is_above:
        return replace(state, above_since_ms=None), []
    above_since = state.above_since_ms if state.above_since_ms is not None else sample.ts_ms
    if sample.ts_ms - above_since >= rules.enter_after_ms:
        return _enter_feed(state, sample), [EpisodeEvent(EventKind.ENTERED_FEED, sample.ts_ms)]
    return replace(state, above_since_ms=above_since), []


def end(state: EpisodeState, ts_ms: int, reason: str) -> tuple[EpisodeState, list[EpisodeEvent]]:
    """Close an episode for an external reason too: delisted, evicted, app_stop."""
    if state.phase is Phase.ENDED:
        return state, []
    ended = replace(state, phase=Phase.ENDED, end_reason=reason)
    return ended, [EpisodeEvent(EventKind.ENDED, ts_ms, reason)]
