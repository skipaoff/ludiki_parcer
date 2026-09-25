-- VFP: History keeps what the screen decides on — funding, expected result and interest — so thresholds can be
-- calibrated on the same numbers a trader saw.
-- Changes when: the feed learns a new decision number worth recording.
-- Anti-goal:
-- 1. Editing an applied migration — the runner compares checksums; a change is a new numbered file.
-- 2. Columns the code never fills: every column below has exactly one writer (PLAN.md, section 11.2).
--
-- Adding nullable columns to compressed hypertables is supported by TimescaleDB; old rows keep NULL, which reads
-- as "this was not recorded yet" rather than zero.

BEGIN;

-- Gap samples: the funding and result numbers behind every feed row, once a second.
ALTER TABLE episode_samples
    ADD COLUMN funding_hourly_pct  NUMERIC,
    ADD COLUMN funding_horizon_pct NUMERIC,
    ADD COLUMN funding_next_pct    NUMERIC,
    ADD COLUMN funding_next_at     TIMESTAMPTZ,
    ADD COLUMN total_pct           NUMERIC,
    ADD COLUMN interest            SMALLINT;

-- Episode: the best the gap ever promised, and what funding looked like when it reached the feed.
ALTER TABLE opportunity_episodes
    ADD COLUMN funding_horizon_pct_first NUMERIC,
    ADD COLUMN total_peak_pct            NUMERIC,
    ADD COLUMN total_peak_at             TIMESTAMPTZ,
    ADD COLUMN interest_peak             SMALLINT;

-- Open pair: what funding is doing to a position that is already on, once a second.
ALTER TABLE position_samples
    ADD COLUMN funding_hourly_usd  NUMERIC,
    ADD COLUMN funding_next_usd    NUMERIC,
    ADD COLUMN funding_next_at     TIMESTAMPTZ,
    ADD COLUMN funding_accrued_usd NUMERIC;

-- Trade: the decision numbers at the moment of the click, to compare what was opened against what was shown.
ALTER TABLE trades
    ADD COLUMN total_pct_at_open           NUMERIC,
    ADD COLUMN funding_horizon_pct_at_open NUMERIC,
    ADD COLUMN interest_at_open            SMALLINT;

-- Radar: the funding rate of every contract once a minute, so a backtest can price holding cost.
ALTER TABLE radar_snapshots
    ADD COLUMN funding_rate_pct NUMERIC,
    ADD COLUMN funding_interval_h NUMERIC,
    ADD COLUMN funding_next_at TIMESTAMPTZ;

COMMIT;
