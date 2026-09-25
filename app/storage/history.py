"""
VFP: Read-side queries over recorded gap history — funnel and lifetime summary, recent episodes, storage growth.
Changes when: the statistics the interface shows about recorded history change.
Anti-goal:
1. Writes here, except closing episodes a crash left open.
2. Scanning raw samples for summaries that episode rows already answer.
"""

from __future__ import annotations

from typing import Any

import asyncpg

HYPERTABLES = ("episode_samples", "radar_snapshots", "position_samples", "latency_samples", "balance_snapshots")


async def close_dangling_episodes(pool: asyncpg.Pool) -> int:
    """Episodes still open in the database belong to a run that did not stop cleanly."""
    status = await pool.execute(
        """
        UPDATE opportunity_episodes
        SET ended_at = coalesce(
                (SELECT max(ts) FROM episode_samples WHERE episode_id = opportunity_episodes.id),
                entered_feed_at, detected_at),
            end_reason = 'app_stop'
        WHERE ended_at IS NULL
        """
    )
    return int(status.rsplit(" ", 1)[-1])


def _plain(row: asyncpg.Record) -> dict[str, Any]:
    result = {}
    for key, value in row.items():
        if hasattr(value, "isoformat"):
            result[key] = int(value.timestamp() * 1000)
        elif value is not None and not isinstance(value, (int, float, str, bool)):
            result[key] = str(value)
        else:
            result[key] = value
    return result


async def summary(pool: asyncpg.Pool, hours: int) -> dict[str, Any]:
    row = await pool.fetchrow(
        """
        SELECT
            count(*)                                                        AS gaps,
            count(*) FILTER (WHERE opened)                                  AS opened,
            count(*) FILTER (WHERE ended_at IS NULL)                        AS live,
            count(*) FILTER (WHERE end_reason = 'converged')                AS converged,
            count(*) FILTER (WHERE suspicious)                              AS suspicious,
            percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM (coalesce(left_feed_at, ended_at) - entered_feed_at)))
                                                                            AS median_in_feed_s,
            percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM (converged_at - entered_feed_at)))
                                                                            AS median_to_converge_s,
            max(roi_peak)                                                   AS best_roi_peak,
            avg(roi_peak)                                                   AS avg_roi_peak,
            avg(missed_pnl_best_pct)                                        AS avg_missed_pnl_pct,
            sum(missed_pnl_best_pct * size_usd / 100)                       AS missed_pnl_usd,
            count(DISTINCT token)                                           AS tokens
        FROM opportunity_episodes
        WHERE entered_feed_at >= now() - make_interval(hours => $1)
        """,
        hours,
    )
    buckets = await pool.fetch(
        """
        SELECT width_bucket(roi_peak, ARRAY[0.5, 1, 2, 5]::numeric[]) AS bucket, count(*) AS gaps,
               avg(extract(epoch FROM (coalesce(left_feed_at, ended_at) - entered_feed_at))) AS avg_in_feed_s
        FROM opportunity_episodes
        WHERE entered_feed_at >= now() - make_interval(hours => $1)
        GROUP BY 1 ORDER BY 1
        """,
        hours,
    )
    return {"hours": hours, **_plain(row), "roi_buckets": [_plain(bucket) for bucket in buckets]}


async def recent_episodes(pool: asyncpg.Pool, limit: int) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        SELECT id::text AS id, token, long_exchange, short_exchange, size_usd, entered_feed_at, left_feed_at, ended_at,
               end_reason, roi_first, roi_peak, capacity_peak_usd, missed_pnl_best_pct, samples_count, suspicious
        FROM opportunity_episodes
        ORDER BY entered_feed_at DESC
        LIMIT $1
        """,
        limit,
    )
    return [_plain(row) for row in rows]


async def storage_usage(pool: asyncpg.Pool) -> dict[str, Any]:
    database_bytes = await pool.fetchval("SELECT pg_database_size(current_database())")
    tables = {}
    for table in HYPERTABLES:
        size = await pool.fetchval("SELECT hypertable_size($1::regclass)", table)
        day = await pool.fetchval(f'SELECT count(*) FROM "{table}" WHERE ts >= now() - interval \'24 hours\'')
        tables[table] = {"bytes": size, "rows_24h": day}
    episodes_day = await pool.fetchval(
        "SELECT count(*) FROM opportunity_episodes WHERE entered_feed_at >= now() - interval '24 hours'"
    )
    return {"database_bytes": database_bytes, "hypertables": tables, "episodes_24h": episodes_day}


async def daily_digest(pool: asyncpg.Pool, hours: int = 24) -> dict[str, Any]:
    """Numbers for the message that sums up a day: how many gaps, the best one, how long they lived, by venue pair."""
    row = await pool.fetchrow(
        """
        SELECT
            count(*)                                                        AS gaps,
            count(*) FILTER (WHERE end_reason = 'converged')                AS converged,
            percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM (coalesce(left_feed_at, ended_at, now()) - entered_feed_at)))
                                                                            AS median_in_feed_s
        FROM opportunity_episodes
        WHERE entered_feed_at >= now() - make_interval(hours => $1)
        """,
        hours,
    )
    best = await pool.fetchrow(
        """
        SELECT token,
               coalesce(total_peak_pct, roi_peak) AS peak_pct,
               extract(epoch FROM (coalesce(left_feed_at, ended_at, now()) - entered_feed_at)) AS in_feed_s
        FROM opportunity_episodes
        WHERE entered_feed_at >= now() - make_interval(hours => $1)
        ORDER BY coalesce(total_peak_pct, roi_peak) DESC NULLS LAST
        LIMIT 1
        """,
        hours,
    )
    pairs = await pool.fetch(
        """
        SELECT upper(long_exchange) || ' ↔ ' || upper(short_exchange) AS pair, count(*) AS gaps
        FROM opportunity_episodes
        WHERE entered_feed_at >= now() - make_interval(hours => $1)
        GROUP BY 1 ORDER BY 2 DESC LIMIT 4
        """,
        hours,
    )
    size = await pool.fetchval("SELECT pg_size_pretty(pg_database_size(current_database()))")
    return {
        **_plain(row),
        "best": _plain(best) if best else None,
        "by_pair": [_plain(pair) for pair in pairs],
        "database_size": size,
    }
