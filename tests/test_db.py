from datetime import datetime, timezone

from engine import db


def test_migrate_is_idempotent():
    conn = db.connect()
    assert db.migrate(conn) == [v for v, _ in db.MIGRATIONS]
    assert db.migrate(conn) == []
    assert db.current_version(conn) == db.latest_version()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"llm_calls", "api_usage", "schema_version"} <= tables


def test_api_usage_counts_today():
    conn = db.get_conn()
    db.record_api_usage(conn, "youtube", "search.list", 100)
    db.record_api_usage(conn, "youtube", "videos.list", 1)
    db.record_api_usage(conn, "other", "x", 50)
    with conn:
        conn.execute(
            "INSERT INTO api_usage (ts, api, endpoint, units) VALUES (?, 'youtube', 'old', 999)",
            ("2000-01-01T00:00:00+00:00",),
        )
    assert db.api_units_today(conn, "youtube") == 101


def test_quota_day_starts_at_pacific_midnight():
    # 2026-07-01 06:30 UTC = 2026-06-30 23:30 PDT → el día de cuota empezó el 30 a las 07:00 UTC
    now = datetime(2026, 7, 1, 6, 30, tzinfo=timezone.utc)
    assert db.quota_day_start_utc(now) == "2026-06-30T07:00:00+00:00"
