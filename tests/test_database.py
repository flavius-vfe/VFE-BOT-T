from vfe_bot.database import Database


def test_pair_and_atomic_approval(tmp_path) -> None:
    db = Database(str(tmp_path / "test.db"))
    db.initialize()
    assert db.get_owner() is None
    assert db.set_owner(10, 20, "owner")
    assert db.is_owner(10, 20)
    assert not db.is_owner(10, 21)

    approval_id = db.create_approval(10, "restart", "plex", {"container_id": "abc"})
    first = db.claim_approval(approval_id, 10)
    assert first is not None
    assert first["payload"]["container_id"] == "abc"
    assert db.claim_approval(approval_id, 10) is None
    db.finish_approval(approval_id, "success")


def test_schedule_is_duplicate_safe_and_tracks_failures(tmp_path) -> None:
    from datetime import UTC, datetime

    db = Database(str(tmp_path / "schedule.db"))
    db.initialize()
    first_id, created = db.add_schedule("plex", "04:00")
    assert created is True
    second_id, created_again = db.add_schedule("plex", "04:00")
    assert second_id == first_id
    assert created_again is False
    assert len(db.list_schedules()) == 1

    db.mark_schedule_attempt(first_id, datetime(2026, 8, 7, 1, 0, tzinfo=UTC))
    db.mark_schedule_failure(first_id, "temporary error")
    schedule = db.list_schedules(True)[0]
    assert schedule["failure_count"] == 1
    assert schedule["last_error"] == "temporary error"

    db.mark_schedule_success(first_id, "2026-08-07")
    schedule = db.list_schedules(True)[0]
    assert schedule["last_run_date"] == "2026-08-07"
    assert schedule["failure_count"] == 0


def test_missing_container_disables_schedule(tmp_path) -> None:
    db = Database(str(tmp_path / "missing.db"))
    db.initialize()
    db.add_schedule("removed", "04:00")
    assert db.disable_schedules_for_missing({"plex"}) == ["removed"]
    assert db.list_schedules(True) == []


def test_old_schedule_schema_is_migrated(tmp_path) -> None:
    import sqlite3

    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute(
            """
            CREATE TABLE schedules (
                id TEXT PRIMARY KEY,
                container_name TEXT NOT NULL,
                time_hhmm TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_run_date TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
    database = Database(str(path))
    database.initialize()
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(schedules)")}
    assert {"last_attempt_at", "last_attempt_date", "failure_count", "last_error"} <= columns
