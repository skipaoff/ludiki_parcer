from pathlib import Path

from app.config.settings import StorageSettings
from app.storage.spool import Spool
from app.storage.writer import WriteQueue


class FakeStore:
    """In-memory stand-in for Database: can go down, refuse rows, and count calls."""

    def __init__(self):
        self.up = True
        self.connected = False
        self.rows: list[tuple[str, dict]] = []
        self.poison: set[int] = set()
        self.fail_after_rows: int | None = None
        self.pings = 0

    @property
    def ready(self):
        return self.connected

    async def connect(self):
        if not self.up:
            raise ConnectionRefusedError("down")
        self.connected = True
        return []

    async def insert_batch(self, rows):
        if not self.up:
            raise ConnectionRefusedError("down")
        if any(row.get("n") in self.poison for _, row in rows):
            raise ValueError("DataError: poison row")
        if self.fail_after_rows is not None and len(self.rows) + len(rows) > self.fail_after_rows:
            self.up = False
            raise ConnectionResetError("dropped mid-flush")
        self.rows.extend(rows)

    async def ping(self):
        self.pings += 1
        if not self.up:
            raise ConnectionRefusedError("down")

    def mark_down(self, error):
        self.connected = False


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make(tmp_path: Path, store: FakeStore, clock: Clock, max_batch_rows: int = 5000):
    notes: list[tuple[str, dict]] = []
    settings = StorageSettings(flush_interval_ms=300, max_batch_rows=max_batch_rows, reconnect_interval_s=5)
    queue = WriteQueue(store, Spool(tmp_path), settings, notify=lambda kind, detail: notes.append((kind, detail)), clock=clock)
    return queue, notes


def numbers(store: FakeStore) -> list[int]:
    return [row["n"] for _, row in store.rows]


async def test_rows_are_written_in_order_and_in_batches(tmp_path):
    store, clock = FakeStore(), Clock()
    queue, notes = make(tmp_path, store, clock, max_batch_rows=2)
    for n in range(5):
        queue.submit("events", {"n": n})

    await queue.flush()

    assert numbers(store) == [0, 1, 2, 3, 4]
    assert queue.pending_rows == 0
    assert notes == [("db_connected", {"migrations_applied": [], "spooled_rows": 0})]


async def test_outage_spools_to_disk_and_replays_before_new_rows(tmp_path):
    store, clock = FakeStore(), Clock()
    store.up = False
    queue, notes = make(tmp_path, store, clock)
    queue.submit("events", {"n": 1})
    queue.submit("events", {"n": 2})

    await queue.flush()
    assert queue.pending_rows == 2 and queue.spooled_rows == 0  # kept in memory until the spool is due

    clock.now += 5
    await queue.flush()
    assert queue.pending_rows == 0 and queue.spooled_rows == 2
    assert notes[-1][0] == "db_unavailable"

    queue.submit("events", {"n": 3})
    store.up = True
    clock.now += 5
    await queue.flush()

    assert numbers(store) == [1, 2, 3]
    assert queue.spooled_rows == 0
    assert list(tmp_path.glob("spool-*.jsonl")) == []
    assert notes[-1] == ("db_connected", {"migrations_applied": [], "spooled_rows": 2})


async def test_spool_survives_a_restart(tmp_path):
    store, clock = FakeStore(), Clock()
    store.up = False
    queue, _ = make(tmp_path, store, clock)
    queue.submit("events", {"n": 7})
    await queue.close()

    store.up = True
    restarted, _ = make(tmp_path, store, clock)
    assert restarted.spooled_rows == 1
    await restarted.flush()

    assert numbers(store) == [7]


async def test_connection_lost_mid_flush_keeps_the_unwritten_tail_in_order(tmp_path):
    store, clock = FakeStore(), Clock()
    store.fail_after_rows = 2
    queue, _ = make(tmp_path, store, clock, max_batch_rows=1)
    for n in range(4):
        queue.submit("events", {"n": n})

    await queue.flush()
    assert numbers(store) == [0, 1]

    store.up, store.fail_after_rows = True, None
    clock.now += 5
    await queue.flush()
    clock.now += 5
    await queue.flush()

    assert numbers(store) == [0, 1, 2, 3]


async def test_rejected_row_is_set_aside_and_the_rest_is_written(tmp_path):
    store, clock = FakeStore(), Clock()
    store.poison = {2}
    queue, _ = make(tmp_path, store, clock)
    for n in range(1, 4):
        queue.submit("events", {"n": n})

    await queue.flush()

    assert numbers(store) == [1, 3]
    assert queue.rejected_rows == 1
    assert len(list(tmp_path.glob("rejected-*.jsonl"))) == 1


async def test_idle_queue_notices_an_outage_by_pinging(tmp_path):
    store, clock = FakeStore(), Clock()
    queue, notes = make(tmp_path, store, clock)
    await queue.flush()
    assert store.ready

    store.up = False
    clock.now += 5
    await queue.flush()

    assert not store.ready
    assert notes[-1][0] == "db_unavailable"


async def test_ping_is_rate_limited(tmp_path):
    store, clock = FakeStore(), Clock()
    queue, _ = make(tmp_path, store, clock)
    for _ in range(3):
        await queue.flush()

    assert store.pings == 1
