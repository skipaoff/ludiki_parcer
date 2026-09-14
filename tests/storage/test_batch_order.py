from app.storage.database import consecutive_groups, upsert_tail


def test_groups_keep_submission_order_and_merge_only_neighbours():
    rows = [
        ("opportunity_episodes", {"id": 1, "opened": False}),
        ("episode_samples", {"episode_id": 1, "ts": 1}),
        ("episode_samples", {"episode_id": 1, "ts": 2}),
        ("trades", {"id": 9}),
        ("opportunity_episodes", {"id": 1, "opened": True}),
    ]
    groups = consecutive_groups(rows)
    assert [(table, len(group)) for table, _, group in groups] == [
        ("opportunity_episodes", 1),
        ("episode_samples", 2),
        ("trades", 1),
        ("opportunity_episodes", 1),
    ]


def test_upsert_tail_updates_everything_but_the_key():
    assert upsert_tail(("id",), ("id", "status")) == 'ON CONFLICT ("id") DO UPDATE SET "status" = EXCLUDED."status"'
    assert upsert_tail(("order_id", "exchange_fill_id"), ("order_id", "exchange_fill_id")).endswith("DO NOTHING")
