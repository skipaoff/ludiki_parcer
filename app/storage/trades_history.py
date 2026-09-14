"""
VFP: Read-side queries over finished trades — the trade journal with filters, CSV export and the trading statistics: results, execution quality, funnel.
Changes when: the Trades or Statistics screens need a new figure or filter (PLAN.md, sections 3.2 and 3.3).
Anti-goal:
1. Writes of any kind.
2. Figures the raw tables cannot reproduce — every number here is a plain aggregate over trades or episodes.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg

FINISHED = ("closed", "leg_failed")

TRADE_COLUMNS = (
    "id", "token", "long_exchange", "short_exchange", "qty_tokens", "size_usd", "opened_at", "closed_at", "status",
    "roi_expected_entry", "roi_actual_entry", "exit_spread_expected", "exit_spread_actual", "entry_long_avg",
    "entry_short_avg", "exit_long_avg", "exit_short_avg", "fees_usd", "funding_usd", "pnl_gross_usd", "pnl_net_usd",
    "pnl_net_pct", "close_reason", "click_to_fill_ms_long", "click_to_fill_ms_short", "notes",
)


@dataclass(frozen=True, slots=True)
class TradeFilter:
    since: datetime
    until: datetime
    token: str | None = None
    long_exchange: str | None = None
    short_exchange: str | None = None
    strategy: str | None = None

    def args(self) -> tuple[Any, ...]:
        return (list(FINISHED), self.since, self.until, self.token, self.long_exchange, self.short_exchange, self.strategy)


WHERE = """
    status::text = ANY($1::text[])
    AND closed_at >= $2 AND closed_at < $3
    AND ($4::text IS NULL OR token = $4)
    AND ($5::text IS NULL OR long_exchange = $5)
    AND ($6::text IS NULL OR short_exchange = $6)
    AND ($7::text IS NULL OR strategy::text = $7)
"""


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _row(record: asyncpg.Record) -> dict[str, Any]:
    return {key: _plain(value) for key, value in record.items()}


async def trades(pool: asyncpg.Pool, flt: TradeFilter, limit: int) -> list[dict[str, Any]]:
    columns = ", ".join("id::text AS id" if column == "id" else column for column in TRADE_COLUMNS)
    rows = await pool.fetch(f"SELECT {columns} FROM trades WHERE {WHERE} ORDER BY closed_at DESC LIMIT $8", *flt.args(), limit)
    return [_row(row) for row in rows]


async def trades_csv(pool: asyncpg.Pool, flt: TradeFilter) -> str:
    rows = await trades(pool, flt, limit=100_000)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(TRADE_COLUMNS)
    for row in rows:
        values = []
        for column in TRADE_COLUMNS:
            value = row[column]
            if column in ("opened_at", "closed_at") and value is not None:
                value = datetime.fromtimestamp(value / 1000).isoformat(sep=" ", timespec="seconds")
            values.append("" if value is None else value)
        writer.writerow(values)
    return buffer.getvalue()


async def trade_stats(pool: asyncpg.Pool, flt: TradeFilter) -> dict[str, Any]:
    args = flt.args()
    totals = await pool.fetchrow(
        f"""
        SELECT count(*) AS trades,
               count(*) FILTER (WHERE status = 'leg_failed') AS failed,
               count(*) FILTER (WHERE pnl_net_usd > 0) AS wins,
               coalesce(sum(pnl_net_usd), 0) AS pnl_net_usd,
               coalesce(sum(fees_usd), 0) AS fees_usd,
               coalesce(sum(funding_usd), 0) AS funding_usd,
               avg(pnl_net_pct) AS avg_pnl_pct,
               avg(extract(epoch FROM closed_at - opened_at)) AS avg_duration_s
        FROM trades WHERE {WHERE}
        """,
        *args,
    )
    slippage = await pool.fetch(
        f"""
        SELECT long_exchange, short_exchange, count(*) AS trades,
               avg(roi_expected_entry - roi_actual_entry) AS avg_slippage_pct,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY roi_expected_entry - roi_actual_entry) AS median_slippage_pct,
               avg(roi_expected_entry) AS avg_expected_pct,
               avg(roi_actual_entry) AS avg_actual_pct
        FROM trades
        WHERE {WHERE} AND roi_expected_entry IS NOT NULL AND roi_actual_entry IS NOT NULL
        GROUP BY 1, 2 ORDER BY 3 DESC
        """,
        *args,
    )
    latency = await pool.fetch(
        f"""
        WITH legs AS (
            SELECT long_exchange AS exchange, click_to_fill_ms_long AS ms FROM trades WHERE {WHERE}
            UNION ALL
            SELECT short_exchange, click_to_fill_ms_short FROM trades WHERE {WHERE}
        )
        SELECT exchange, count(ms) AS orders, avg(ms) AS avg_ms,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY ms) AS median_ms,
               percentile_cont(0.9) WITHIN GROUP (ORDER BY ms) AS p90_ms
        FROM legs WHERE ms IS NOT NULL GROUP BY 1 ORDER BY 1
        """,
        *args,
    )
    by_day = await pool.fetch(
        f"""
        SELECT date_trunc('day', closed_at) AS day, count(*) AS trades, sum(pnl_net_usd) AS pnl_net_usd
        FROM trades WHERE {WHERE} GROUP BY 1 ORDER BY 1
        """,
        *args,
    )
    by_token = await pool.fetch(
        f"""
        SELECT token, count(*) AS trades, sum(pnl_net_usd) AS pnl_net_usd, avg(pnl_net_pct) AS avg_pnl_pct
        FROM trades WHERE {WHERE} GROUP BY 1 ORDER BY 3 DESC NULLS LAST LIMIT 30
        """,
        *args,
    )
    by_pair = await pool.fetch(
        f"""
        SELECT long_exchange, short_exchange, count(*) AS trades, sum(pnl_net_usd) AS pnl_net_usd
        FROM trades WHERE {WHERE} GROUP BY 1, 2 ORDER BY 4 DESC NULLS LAST
        """,
        *args,
    )
    by_hour = await pool.fetch(
        f"""
        SELECT extract(hour FROM opened_at AT TIME ZONE current_setting('TimeZone'))::int AS hour,
               count(*) AS trades, sum(pnl_net_usd) AS pnl_net_usd
        FROM trades WHERE {WHERE} GROUP BY 1 ORDER BY 1
        """,
        *args,
    )
    funnel = await pool.fetch(
        """
        SELECT width_bucket(roi_peak, ARRAY[0.5, 1, 2, 5]::numeric[]) AS bucket,
               count(*) AS gaps, count(*) FILTER (WHERE opened) AS opened
        FROM opportunity_episodes
        WHERE entered_feed_at >= $1 AND entered_feed_at < $2
        GROUP BY 1 ORDER BY 1
        """,
        flt.since,
        flt.until,
    )
    periods = await pool.fetchrow(
        """
        SELECT coalesce(sum(pnl_net_usd) FILTER (WHERE closed_at >= date_trunc('day', now())), 0) AS today,
               coalesce(sum(pnl_net_usd) FILTER (WHERE closed_at >= now() - interval '7 days'), 0) AS week,
               coalesce(sum(pnl_net_usd) FILTER (WHERE closed_at >= now() - interval '30 days'), 0) AS month
        FROM trades WHERE status::text = ANY($1::text[])
        """,
        list(FINISHED),
    )
    total_trades = totals["trades"] or 0
    return {
        "totals": {**_row(totals), "win_rate": (totals["wins"] / total_trades) if total_trades else None},
        "periods": _row(periods),
        "slippage": [_row(row) for row in slippage],
        "latency": [_row(row) for row in latency],
        "by_day": [_row(row) for row in by_day],
        "by_token": [_row(row) for row in by_token],
        "by_pair": [_row(row) for row in by_pair],
        "by_hour": [_row(row) for row in by_hour],
        "funnel": [_row(row) for row in funnel],
    }
