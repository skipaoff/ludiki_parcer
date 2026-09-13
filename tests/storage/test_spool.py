from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import orjson

from app.storage.spool import Spool, decode_row, encode_row


def test_row_round_trip_keeps_decimal_and_datetime_types():
    row = {
        "ts": datetime(2026, 9, 13, 12, 0, 0, 123000, tzinfo=UTC),
        "roi_net_pct": Decimal("0.123456789012345678"),
        "payload": {"error": "refused", "n": 3},
        "trade_id": None,
    }

    table, decoded = decode_row(encode_row("episode_samples", row))

    assert table == "episode_samples"
    assert decoded == row
    assert isinstance(decoded["roi_net_pct"], Decimal)


def test_files_replay_in_write_order_and_replace_keeps_position(tmp_path: Path):
    spool = Spool(tmp_path)
    first = spool.append([("events", {"n": 1}), ("events", {"n": 2})])
    second = spool.append([("events", {"n": 3})])

    spool.replace(first, [("events", {"n": 2})])

    assert spool.files() == [first, second]
    assert [row["n"] for path in spool.files() for _, row in spool.read(path)] == [2, 3]
    assert spool.row_count() == 2


def test_rejected_rows_are_kept_apart_with_the_error(tmp_path: Path):
    spool = Spool(tmp_path)

    spool.reject([("events", {"n": 1})], "DataError: bad")

    assert spool.files() == []
    rejected = list(tmp_path.glob("rejected-*.jsonl"))
    assert len(rejected) == 1
    assert orjson.loads(rejected[0].read_bytes())["error"] == "DataError: bad"
