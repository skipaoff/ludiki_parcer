-- VFP: Storage schema for the gap terminal — instruments, episodes of gaps (opened or not), trades, orders and journals.
-- Changes when: the recorded facts change; every change after the first release goes into a new numbered migration.
-- Anti-goal:
-- 1. API keys, secrets or request signatures in any column.
-- 2. Strategy-specific tables for price gaps only — funding and unlocks reuse the same tables via `strategy`.
--
-- Plain PostgreSQL (checked on 18.4). Time-series extras for TimescaleDB are in 002_timescale.sql.
-- Specification: docs/PLAN.md, section 11.

BEGIN;

CREATE TYPE strategy_kind AS ENUM ('price_gap', 'funding', 'unlocks');
CREATE TYPE leg_side AS ENUM ('long', 'short');
CREATE TYPE trade_status AS ENUM ('opening', 'open', 'closing', 'closed', 'leg_failed', 'leg_lost');
CREATE TYPE episode_end_reason AS ENUM ('converged', 'timeout', 'delisted', 'evicted', 'app_stop');
CREATE TYPE order_purpose AS ENUM ('open_long', 'open_short', 'close_long', 'close_short', 'fix_leg');
CREATE TYPE close_reason AS ENUM ('manual', 'close_all', 'auto', 'leg_failure', 'liquidation', 'external');
CREATE TYPE event_level AS ENUM ('info', 'warning', 'critical');

-- ─── Instruments ──────────────────────────────────────────────────────────────

CREATE TABLE instruments (
    id                    BIGSERIAL PRIMARY KEY,
    exchange              TEXT        NOT NULL,
    symbol_raw            TEXT        NOT NULL,
    token                 TEXT        NOT NULL,
    qty_unit_tokens       NUMERIC     NOT NULL CHECK (qty_unit_tokens > 0),
    price_unit_tokens     NUMERIC     NOT NULL CHECK (price_unit_tokens > 0),
    qty_step_units        NUMERIC     NOT NULL CHECK (qty_step_units > 0),
    min_qty_units         NUMERIC     NOT NULL,
    max_market_qty_units  NUMERIC,
    min_notional_usd      NUMERIC     NOT NULL DEFAULT 0,
    price_tick            NUMERIC,
    is_active             BOOLEAN     NOT NULL DEFAULT TRUE,
    listed_at             TIMESTAMPTZ,
    delisted_at           TIMESTAMPTZ,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (exchange, symbol_raw)
);
CREATE INDEX instruments_token_idx ON instruments (token);

CREATE TABLE pairs (
    id                      BIGSERIAL PRIMARY KEY,
    token                   TEXT        NOT NULL,
    instrument_a_id         BIGINT      NOT NULL REFERENCES instruments (id),
    instrument_b_id         BIGINT      NOT NULL REFERENCES instruments (id),
    common_qty_step_tokens  NUMERIC     NOT NULL CHECK (common_qty_step_tokens > 0),
    suspicious              BOOLEAN     NOT NULL DEFAULT FALSE,
    suspicious_reason       TEXT,
    manually_verified       BOOLEAN     NOT NULL DEFAULT FALSE,
    blacklisted             BOOLEAN     NOT NULL DEFAULT FALSE,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (instrument_a_id < instrument_b_id),
    UNIQUE (instrument_a_id, instrument_b_id)
);
CREATE INDEX pairs_token_idx ON pairs (token);

CREATE TABLE fee_rates (
    id             BIGSERIAL PRIMARY KEY,
    exchange       TEXT        NOT NULL,
    instrument_id  BIGINT      REFERENCES instruments (id),
    taker_pct      NUMERIC     NOT NULL,
    maker_pct      NUMERIC     NOT NULL,
    source         TEXT        NOT NULL CHECK (source IN ('account', 'api', 'manual')),
    valid_from     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX fee_rates_lookup_idx ON fee_rates (exchange, instrument_id, valid_from DESC);

-- ─── Trades ───────────────────────────────────────────────────────────────────

CREATE TABLE trades (
    id                      BIGSERIAL PRIMARY KEY,
    strategy                strategy_kind NOT NULL,
    episode_id              BIGINT,
    pair_id                 BIGINT        REFERENCES pairs (id),
    token                   TEXT          NOT NULL,
    long_exchange           TEXT          NOT NULL,
    short_exchange          TEXT          NOT NULL,
    qty_tokens              NUMERIC       NOT NULL CHECK (qty_tokens >= 0),
    size_usd                NUMERIC       NOT NULL,
    leverage_long           INTEGER       NOT NULL,
    leverage_short          INTEGER       NOT NULL,
    margin_mode             TEXT          NOT NULL CHECK (margin_mode IN ('isolated', 'cross')),
    status                  trade_status  NOT NULL DEFAULT 'opening',
    opened_at               TIMESTAMPTZ   NOT NULL DEFAULT now(),
    closed_at               TIMESTAMPTZ,
    roi_expected_entry      NUMERIC,
    roi_actual_entry        NUMERIC,
    entry_long_avg          NUMERIC,
    entry_short_avg         NUMERIC,
    exit_spread_expected    NUMERIC,
    exit_spread_actual      NUMERIC,
    exit_long_avg           NUMERIC,
    exit_short_avg          NUMERIC,
    fees_usd                NUMERIC       NOT NULL DEFAULT 0,
    funding_usd             NUMERIC       NOT NULL DEFAULT 0,
    pnl_gross_usd           NUMERIC,
    pnl_net_usd             NUMERIC,
    pnl_net_pct             NUMERIC,
    close_reason            close_reason,
    click_to_fill_ms_long   INTEGER,
    click_to_fill_ms_short  INTEGER,
    settings_snapshot       JSONB         NOT NULL,
    notes                   TEXT
);
CREATE INDEX trades_opened_at_idx ON trades (opened_at DESC);
CREATE INDEX trades_status_idx ON trades (status) WHERE status IN ('opening', 'open', 'closing', 'leg_lost');
CREATE INDEX trades_token_idx ON trades (token, opened_at DESC);

-- ─── Episodes: every gap we saw, opened or not ────────────────────────────────

CREATE TABLE opportunity_episodes (
    id                    BIGSERIAL PRIMARY KEY,
    strategy              strategy_kind      NOT NULL,
    pair_id               BIGINT             REFERENCES pairs (id),
    token                 TEXT               NOT NULL,
    long_exchange         TEXT               NOT NULL,
    short_exchange        TEXT               NOT NULL,
    size_usd              NUMERIC            NOT NULL,
    detected_at           TIMESTAMPTZ        NOT NULL,
    entered_feed_at       TIMESTAMPTZ,
    left_feed_at          TIMESTAMPTZ,
    converged_at          TIMESTAMPTZ,
    ended_at              TIMESTAMPTZ,
    end_reason            episode_end_reason,
    roi_first             NUMERIC,
    roi_peak              NUMERIC,
    roi_peak_at           TIMESTAMPTZ,
    capacity_peak_usd     NUMERIC,
    volume24h_long_usd    NUMERIC,
    volume24h_short_usd   NUMERIC,
    index_diff_pct        NUMERIC,
    suspicious            BOOLEAN            NOT NULL DEFAULT FALSE,
    opened                BOOLEAN            NOT NULL DEFAULT FALSE,
    trade_id              BIGINT             REFERENCES trades (id),
    missed_pnl_best_pct   NUMERIC,
    missed_best_exit_at   TIMESTAMPTZ,
    samples_count         INTEGER            NOT NULL DEFAULT 0,
    settings_snapshot     JSONB              NOT NULL
);
CREATE INDEX episodes_detected_at_idx ON opportunity_episodes (detected_at DESC);
CREATE INDEX episodes_token_idx ON opportunity_episodes (token, detected_at DESC);
CREATE INDEX episodes_opened_idx ON opportunity_episodes (opened, detected_at DESC);
CREATE INDEX episodes_live_idx ON opportunity_episodes (id) WHERE ended_at IS NULL;

ALTER TABLE trades
    ADD CONSTRAINT trades_episode_fk FOREIGN KEY (episode_id) REFERENCES opportunity_episodes (id);

CREATE TABLE episode_samples (
    ts                    TIMESTAMPTZ NOT NULL,
    episode_id            BIGINT      NOT NULL REFERENCES opportunity_episodes (id),
    ask_long_vwap         NUMERIC,
    bid_short_vwap        NUMERIC,
    roi_gross_pct         NUMERIC,
    roi_net_pct           NUMERIC,
    capacity_usd          NUMERIC,
    bid_long_exit_vwap    NUMERIC,
    ask_short_exit_vwap   NUMERIC,
    exit_spread_pct       NUMERIC,
    age_long_ms           INTEGER,
    age_short_ms          INTEGER
);
CREATE INDEX episode_samples_episode_ts_idx ON episode_samples (episode_id, ts);

CREATE TABLE radar_snapshots (
    ts              TIMESTAMPTZ NOT NULL,
    instrument_id   BIGINT      NOT NULL REFERENCES instruments (id),
    bid             NUMERIC,
    ask             NUMERIC,
    mark            NUMERIC,
    index_price     NUMERIC,
    volume24h_usd   NUMERIC
);
CREATE INDEX radar_snapshots_instrument_ts_idx ON radar_snapshots (instrument_id, ts);

-- ─── Orders and fills ─────────────────────────────────────────────────────────

CREATE TABLE orders (
    id                   BIGSERIAL PRIMARY KEY,
    trade_id             BIGINT        NOT NULL REFERENCES trades (id),
    exchange             TEXT          NOT NULL,
    instrument_id        BIGINT        REFERENCES instruments (id),
    client_order_id      TEXT          NOT NULL UNIQUE,
    exchange_order_id    TEXT,
    purpose              order_purpose NOT NULL,
    side                 leg_side      NOT NULL,
    qty_tokens           NUMERIC       NOT NULL,
    qty_exchange_units   NUMERIC       NOT NULL,
    order_type           TEXT          NOT NULL DEFAULT 'market',
    reduce_only          BOOLEAN       NOT NULL,
    clicked_at           TIMESTAMPTZ,
    sent_at              TIMESTAMPTZ   NOT NULL,
    ack_at               TIMESTAMPTZ,
    filled_at            TIMESTAMPTZ,
    status               TEXT          NOT NULL,
    filled_qty_tokens    NUMERIC       NOT NULL DEFAULT 0,
    avg_price            NUMERIC,
    fee_usd              NUMERIC,
    error_code           TEXT,
    error_message        TEXT,
    request              JSONB,
    response             JSONB
);
CREATE INDEX orders_trade_idx ON orders (trade_id);

CREATE TABLE fills (
    id                BIGSERIAL PRIMARY KEY,
    order_id          BIGINT      NOT NULL REFERENCES orders (id),
    exchange_fill_id  TEXT,
    ts                TIMESTAMPTZ NOT NULL,
    price             NUMERIC     NOT NULL,
    qty_tokens        NUMERIC     NOT NULL,
    fee               NUMERIC,
    fee_asset         TEXT,
    is_maker          BOOLEAN,
    UNIQUE (order_id, exchange_fill_id)
);

-- ─── Open positions, funding, balances ────────────────────────────────────────

CREATE TABLE position_samples (
    ts                    TIMESTAMPTZ NOT NULL,
    trade_id              BIGINT      NOT NULL REFERENCES trades (id),
    mark_long             NUMERIC,
    mark_short            NUMERIC,
    bid_long_exit_vwap    NUMERIC,
    ask_short_exit_vwap   NUMERIC,
    exit_spread_pct       NUMERIC,
    pnl_now_usd           NUMERIC,
    liq_dist_long_pct     NUMERIC,
    liq_dist_short_pct    NUMERIC
);
CREATE INDEX position_samples_trade_ts_idx ON position_samples (trade_id, ts);

CREATE TABLE funding_payments (
    id          BIGSERIAL PRIMARY KEY,
    trade_id    BIGINT      NOT NULL REFERENCES trades (id),
    exchange    TEXT        NOT NULL,
    ts          TIMESTAMPTZ NOT NULL,
    rate        NUMERIC,
    amount_usd  NUMERIC     NOT NULL
);
CREATE INDEX funding_payments_trade_idx ON funding_payments (trade_id);

CREATE TABLE balance_snapshots (
    ts               TIMESTAMPTZ NOT NULL,
    exchange         TEXT        NOT NULL,
    equity_usd       NUMERIC     NOT NULL,
    available_usd    NUMERIC     NOT NULL,
    margin_used_usd  NUMERIC     NOT NULL
);
CREATE INDEX balance_snapshots_exchange_ts_idx ON balance_snapshots (exchange, ts);

-- ─── Journals ─────────────────────────────────────────────────────────────────

CREATE TABLE latency_samples (
    ts        TIMESTAMPTZ NOT NULL,
    exchange  TEXT        NOT NULL,
    kind      TEXT        NOT NULL CHECK (kind IN ('rest_ping', 'ws_lag', 'order_ack', 'order_fill')),
    ms        INTEGER     NOT NULL
);
CREATE INDEX latency_samples_exchange_ts_idx ON latency_samples (exchange, kind, ts);

CREATE TABLE events (
    id        BIGSERIAL PRIMARY KEY,
    ts        TIMESTAMPTZ NOT NULL DEFAULT now(),
    level     event_level NOT NULL,
    source    TEXT        NOT NULL,
    type      TEXT        NOT NULL,
    exchange  TEXT,
    trade_id  BIGINT      REFERENCES trades (id),
    payload   JSONB
);
CREATE INDEX events_ts_idx ON events (ts DESC);
CREATE INDEX events_trade_idx ON events (trade_id) WHERE trade_id IS NOT NULL;

CREATE TABLE settings_history (
    id         BIGSERIAL PRIMARY KEY,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    key        TEXT        NOT NULL,
    old_value  JSONB,
    new_value  JSONB
);

COMMIT;
