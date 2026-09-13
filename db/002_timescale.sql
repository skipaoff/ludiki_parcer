-- VFP: Time-series storage policy — hypertables, compression, retention and rollups for high-frequency tables.
-- Changes when: sampling rates or how long raw samples are kept change.
-- Anti-goal:
-- 1. Retention on trades, orders, fills, episodes or events — those are kept forever.
--
-- Requires the TimescaleDB extension. CI applies this file on timescale/timescaledb:2.30.0-pg18.
-- Hypertables require the time column in every unique index; the sample tables have no primary key for that reason.

CREATE EXTENSION IF NOT EXISTS timescaledb;

SELECT create_hypertable('episode_samples',  by_range('ts', INTERVAL '1 day'), migrate_data => TRUE);
SELECT create_hypertable('radar_snapshots',  by_range('ts', INTERVAL '1 day'), migrate_data => TRUE);
SELECT create_hypertable('position_samples', by_range('ts', INTERVAL '1 day'), migrate_data => TRUE);
SELECT create_hypertable('latency_samples',  by_range('ts', INTERVAL '1 day'), migrate_data => TRUE);
SELECT create_hypertable('balance_snapshots', by_range('ts', INTERVAL '7 days'), migrate_data => TRUE);

ALTER TABLE episode_samples  SET (timescaledb.compress, timescaledb.compress_segmentby = 'episode_id');
ALTER TABLE radar_snapshots  SET (timescaledb.compress, timescaledb.compress_segmentby = 'instrument_id');
ALTER TABLE position_samples SET (timescaledb.compress, timescaledb.compress_segmentby = 'trade_id');
ALTER TABLE latency_samples  SET (timescaledb.compress, timescaledb.compress_segmentby = 'exchange, kind');

SELECT add_compression_policy('episode_samples',  INTERVAL '7 days');
SELECT add_compression_policy('radar_snapshots',  INTERVAL '7 days');
SELECT add_compression_policy('position_samples', INTERVAL '7 days');
SELECT add_compression_policy('latency_samples',  INTERVAL '7 days');

-- Minute rollup of gap samples, kept forever after raw samples expire.
CREATE MATERIALIZED VIEW episode_samples_1m
WITH (timescaledb.continuous) AS
SELECT
    time_bucket(INTERVAL '1 minute', ts) AS bucket,
    episode_id,
    max(roi_net_pct)      AS roi_net_max,
    avg(roi_net_pct)      AS roi_net_avg,
    min(exit_spread_pct)  AS exit_spread_min,
    max(capacity_usd)     AS capacity_max_usd,
    count(*)              AS samples
FROM episode_samples
GROUP BY bucket, episode_id
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'episode_samples_1m',
    start_offset      => INTERVAL '3 hours',
    end_offset        => INTERVAL '1 minute',
    schedule_interval => INTERVAL '5 minutes'
);

-- Daily rollup of latency per exchange, kept forever.
CREATE MATERIALIZED VIEW latency_1d
WITH (timescaledb.continuous) AS
SELECT
    time_bucket(INTERVAL '1 day', ts) AS bucket,
    exchange,
    kind,
    avg(ms)  AS avg_ms,
    max(ms)  AS max_ms,
    count(*) AS samples
FROM latency_samples
GROUP BY bucket, exchange, kind
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'latency_1d',
    start_offset      => INTERVAL '3 days',
    end_offset        => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour'
);

SELECT add_retention_policy('episode_samples',  INTERVAL '90 days');
SELECT add_retention_policy('radar_snapshots',  INTERVAL '90 days');
SELECT add_retention_policy('position_samples', INTERVAL '90 days');
SELECT add_retention_policy('latency_samples',  INTERVAL '90 days');
