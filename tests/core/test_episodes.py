from decimal import Decimal

from app.core.episodes import EpisodeRules, EventKind, Phase, Sample, end, step
from tests.core.helpers import D

# Fast rules for the tests about everything except how many readings the feed demands: one is enough.
RULES = EpisodeRules(min_roi_net_pct=D("0.50"), enter_after_ms=300, enter_min_samples=1)
# The shipped rules: 30 s and five readings above the threshold.
SLOW = EpisodeRules(min_roi_net_pct=D("0.50"))


def run(samples, state=None, rules=RULES):
    events = []
    for ts, roi, exit_spread in samples:
        state, produced = step(state, Sample(ts, None if roi is None else D(roi), None if exit_spread is None else D(exit_spread)), rules)
        events.extend(produced)
    return state, events


def kinds(events):
    return [event.kind for event in events]


def test_flicker_shorter_than_enter_delay_leaves_no_episode():
    state, events = run([(0, "0.8", "1.2"), (100, "0.8", "1.2"), (200, "0.3", "1.0"), (2_300, "0.3", "1.0")])
    assert state is None
    assert events == []


def test_gap_must_live_thirty_seconds_before_it_is_shown():
    one_second = [(ms, "0.9", "1.2") for ms in range(0, 29_000, 1_000)]
    state, events = run(one_second, rules=SLOW)
    assert state.phase is Phase.CANDIDATE and events == []

    state, events = run([(30_000, "0.9", "1.2")], state, SLOW)
    assert kinds(events) == [EventKind.ENTERED_FEED]
    assert state.detected_ms == 0 and state.first_entered_feed_ms == 30_000


def test_time_alone_is_not_enough_without_enough_readings():
    """A pair whose books went quiet must not ride out the wait on one lucky reading."""
    state, events = run([(0, "0.9", "1.2"), (31_000, "0.9", "1.2")], rules=SLOW)

    assert state.phase is Phase.CANDIDATE and events == []
    assert state.above_samples == 2

    # Three more readings above the threshold complete the five.
    state, events = run([(32_000, "0.9", "1.2"), (33_000, "0.9", "1.2"), (34_000, "0.9", "1.2")], state, SLOW)
    assert kinds(events) == [EventKind.ENTERED_FEED]


def test_readings_alone_are_not_enough_without_the_time():
    state, events = run([(ms, "0.9", "1.2") for ms in range(0, 1_200, 100)], rules=SLOW)

    assert state.above_samples >= 5
    assert state.phase is Phase.CANDIDATE and events == []


def test_a_gap_far_above_the_threshold_waits_only_the_short_time():
    # 2.0 % is four times the 0.50 % threshold: such a gap converges fastest and is the one worth clicking.
    state, events = run([(ms, "2.0", "2.5") for ms in range(0, 6_000, 1_000)], rules=SLOW)

    assert state.phase is Phase.IN_FEED
    assert kinds(events) == [EventKind.ENTERED_FEED]


def test_just_under_the_fast_multiple_still_waits_the_full_time():
    state, events = run([(ms, "1.9", "2.5") for ms in range(0, 6_000, 1_000)], rules=SLOW)

    assert state.phase is Phase.CANDIDATE and events == []


def test_a_growing_gap_enters_as_soon_as_it_is_far_enough_above():
    """It has been above the threshold for 10 s already; reaching 2 % makes 5 s the bar it has to clear."""
    grows = [(ms, "0.8", "1.2") for ms in range(0, 10_000, 1_000)] + [(10_000, "2.4", "2.8")]
    state, events = run(grows, rules=SLOW)

    assert state.phase is Phase.IN_FEED
    assert kinds(events) == [EventKind.ENTERED_FEED]


def test_the_fast_path_can_be_turned_off():
    off = EpisodeRules(min_roi_net_pct=D("0.50"), fast_enter_multiple=D(0))
    state, events = run([(ms, "5.0", "5.5") for ms in range(0, 6_000, 1_000)], rules=off)

    assert state.phase is Phase.CANDIDATE and events == []


def test_the_fast_path_never_delays_a_feed_that_is_already_faster():
    # RULES enters after 300 ms; a huge gap must not be held back to the 5 s of the fast path.
    state, events = run([(0, "9.0", "9.5"), (300, "9.0", "9.5")])

    assert state.phase is Phase.IN_FEED
    assert kinds(events) == [EventKind.ENTERED_FEED]


def test_short_dip_does_not_reset_the_entry_clock_but_a_long_one_does():
    # Readings above the threshold at 0, 10 s, 20 s, 22 s and 45 s: five, so only the clock is under test here.
    dip = [
        (0, "0.9", "1.2"),
        (10_000, "0.9", "1.2"),
        (20_000, "0.9", "1.2"),
        (20_500, "0.2", "1.0"),
        (21_500, None, None),
        (22_000, "0.9", "1.2"),
    ]
    state, _ = run(dip, rules=SLOW)
    state, events = run([(45_000, "0.9", "1.2")], state, SLOW)
    assert kinds(events) == [EventKind.ENTERED_FEED]

    long_dip = [(0, "0.9", "1.2"), (20_000, "0.9", "1.2"), (20_500, "0.2", "1.0"), (22_600, "0.2", "1.0")]
    state, events = run(long_dip, rules=SLOW)
    assert state is None and events == []


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
