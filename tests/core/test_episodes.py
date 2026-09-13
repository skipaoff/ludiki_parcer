from decimal import Decimal

from app.core.episodes import EpisodeRules, EventKind, Phase, Sample, end, step
from tests.core.helpers import D

RULES = EpisodeRules(min_roi_net_pct=D("0.50"))


def run(samples, state=None):
    events = []
    for ts, roi, exit_spread in samples:
        state, produced = step(state, Sample(ts, None if roi is None else D(roi), None if exit_spread is None else D(exit_spread)), RULES)
        events.extend(produced)
    return state, events


def kinds(events):
    return [event.kind for event in events]


def test_flicker_shorter_than_enter_delay_leaves_no_episode():
    state, events = run([(0, "0.8", "1.2"), (100, "0.8", "1.2"), (200, "0.3", "1.0")])
    assert state is None
    assert events == []


def test_gap_enters_feed_after_delay_and_tracks_peak():
    state, events = run([(0, "0.8", "1.2"), (300, "0.9", "1.2"), (600, "1.4", "1.6")])
    assert state.phase is Phase.IN_FEED
    assert kinds(events) == [EventKind.ENTERED_FEED]
    assert state.roi_at_feed_entry == D("0.9")
    assert (state.roi_peak, state.roi_peak_ms) == (D("1.4"), 600)


def test_hysteresis_keeps_gap_in_feed_slightly_below_threshold():
    state, events = run([(0, "0.8", "1.2"), (300, "0.8", "1.2"), (1_000, "0.45", "1.0"), (5_000, "0.41", "1.0")])
    assert state.phase is Phase.IN_FEED
    assert kinds(events) == [EventKind.ENTERED_FEED]


def test_gap_leaves_feed_after_exit_delay_and_ends_on_convergence():
    state, events = run(
        [
            (0, "0.8", "1.2"),
            (300, "0.8", "1.2"),
            (1_000, "0.1", "0.6"),
            (3_000, "0.1", "0.4"),
            (60_000, "-0.5", "0.2"),
            (90_000, "-0.8", "-0.05"),
        ]
    )
    assert kinds(events) == [EventKind.ENTERED_FEED, EventKind.LEFT_FEED, EventKind.ENDED]
    assert state.phase is Phase.ENDED
    assert state.end_reason == "converged"
    # entered at 0.8 net, best exit spread -0.05 → missed pnl 0.85%
    assert (state.missed_pnl_best_pct, state.missed_best_exit_ms) == (D("0.85"), 90_000)


def test_tracked_gap_can_reenter_feed_without_resetting_entry_roi():
    state, events = run(
        [(0, "0.8", "1.2"), (300, "0.8", "1.2"), (1_000, "0.1", "0.6"), (3_000, "0.1", "0.6"), (4_000, "1.0", "1.3"), (4_300, "1.0", "1.3")]
    )
    assert kinds(events) == [EventKind.ENTERED_FEED, EventKind.LEFT_FEED, EventKind.ENTERED_FEED]
    assert state.phase is Phase.IN_FEED
    assert state.first_entered_feed_ms == 300
    assert state.roi_at_feed_entry == D("0.8")


def test_insufficient_depth_counts_as_below_threshold():
    state, events = run([(0, "0.8", "1.2"), (300, "0.8", "1.2"), (1_000, None, None), (3_000, None, None)])
    assert kinds(events) == [EventKind.ENTERED_FEED, EventKind.LEFT_FEED]
    assert state.phase is Phase.TRACKING


def test_timeout_and_external_end():
    state, _ = run([(0, "0.8", "1.2"), (300, "0.8", "1.2")])
    timed_out, events = step(state, Sample(RULES.max_lifetime_ms, Decimal("0.9"), Decimal("1.0")), RULES)
    assert (timed_out.end_reason, kinds(events)) == ("timeout", [EventKind.ENDED])

    stopped, events = end(state, 500, "app_stop")
    assert stopped.end_reason == "app_stop"
    assert end(stopped, 600, "app_stop") == (stopped, [])
