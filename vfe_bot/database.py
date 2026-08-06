from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


class Database:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _columns(db: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})").fetchall()}

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS owner (
                    user_id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    username TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    target TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    target TEXT,
                    status TEXT NOT NULL,
                    detail TEXT
                );

                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY,
                    container_name TEXT NOT NULL,
                    time_hhmm TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_run_date TEXT,
                    created_at TEXT NOT NULL,
                    last_attempt_at TEXT,
                    last_attempt_date TEXT,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                );
                """
            )
            columns = self._columns(db, "schedules")
            migrations = {
                "last_attempt_at": "ALTER TABLE schedules ADD COLUMN last_attempt_at TEXT",
                "last_attempt_date": "ALTER TABLE schedules ADD COLUMN last_attempt_date TEXT",
                "failure_count": "ALTER TABLE schedules ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0",
                "last_error": "ALTER TABLE schedules ADD COLUMN last_error TEXT",
            }
            for column, statement in migrations.items():
                if column not in columns:
                    db.execute(statement)
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_schedules_enabled_time ON schedules(enabled, time_hhmm)"
            )

    def get_owner(self) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM owner LIMIT 1").fetchone()
            return dict(row) if row else None

    def set_owner(self, user_id: int, chat_id: int, username: str | None) -> bool:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT user_id FROM owner LIMIT 1").fetchone()
            if existing:
                return int(existing["user_id"]) == user_id
            db.execute(
                "INSERT INTO owner(user_id, chat_id, username, created_at) VALUES (?, ?, ?, ?)",
                (user_id, chat_id, username, iso(utcnow())),
            )
            return True

    def clear_owner(self, user_id: int) -> bool:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = db.execute("SELECT user_id FROM owner LIMIT 1").fetchone()
            if not owner or int(owner["user_id"]) != user_id:
                return False
            db.execute("DELETE FROM approvals")
            db.execute("DELETE FROM owner")
            return True

    def is_owner(self, user_id: int, chat_id: int) -> bool:
        owner = self.get_owner()
        return bool(owner and int(owner["user_id"]) == user_id and int(owner["chat_id"]) == chat_id)

    def create_approval(
        self,
        user_id: int,
        action: str,
        target: str,
        payload: dict[str, Any] | None = None,
        ttl_seconds: int = 180,
    ) -> str:
        approval_id = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8]
        now = utcnow()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO approvals(id, user_id, action, target, payload, expires_at, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    approval_id,
                    user_id,
                    action,
                    target,
                    json.dumps(payload or {}, separators=(",", ":")),
                    iso(now + timedelta(seconds=ttl_seconds)),
                    iso(now),
                ),
            )
        return approval_id

    def claim_approval(self, approval_id: str, user_id: int) -> dict[str, Any] | None:
        now = utcnow()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM approvals WHERE id = ? AND user_id = ?",
                (approval_id, user_id),
            ).fetchone()
            if not row or row["status"] != "pending":
                return None
            if datetime.fromisoformat(row["expires_at"]) <= now:
                db.execute(
                    "UPDATE approvals SET status='expired', completed_at=? WHERE id=? AND status='pending'",
                    (iso(now), approval_id),
                )
                return None
            updated = db.execute(
                "UPDATE approvals SET status='executing' WHERE id=? AND status='pending'",
                (approval_id,),
            )
            if updated.rowcount != 1:
                return None
            result = dict(row)
            result["payload"] = json.loads(result["payload"])
            return result

    def finish_approval(self, approval_id: str, status: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE approvals SET status=?, completed_at=? WHERE id=? AND status='executing'",
                (status, iso(utcnow()), approval_id),
            )

    def deny_approval(self, approval_id: str, user_id: int) -> bool:
        with self.connect() as db:
            updated = db.execute(
                """
                UPDATE approvals SET status='denied', completed_at=?
                WHERE id=? AND user_id=? AND status='pending'
                """,
                (iso(utcnow()), approval_id, user_id),
            )
            return updated.rowcount == 1

    def audit(self, user_id: int, action: str, target: str | None, status: str, detail: str = "") -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO audit(created_at, user_id, action, target, status, detail) VALUES (?, ?, ?, ?, ?, ?)",
                (iso(utcnow()), user_id, action, target, status, detail[:2000]),
            )

    def list_audit(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))
        with self.connect() as db:
            rows = db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(row) for row in rows]

    def add_schedule(self, container_name: str, time_hhmm: str) -> tuple[str, bool]:
        """Add or re-enable a unique container/time schedule.

        Returns (schedule_id, created_new).
        """
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id, enabled FROM schedules WHERE container_name=? AND time_hhmm=? ORDER BY created_at LIMIT 1",
                (container_name, time_hhmm),
            ).fetchone()
            if row:
                db.execute(
                    "UPDATE schedules SET enabled=1, failure_count=0, last_error=NULL WHERE id=?",
                    (row["id"],),
                )
                return str(row["id"]), False
            schedule_id = secrets.token_hex(3)
            db.execute(
                """
                INSERT INTO schedules(id, container_name, time_hhmm, enabled, created_at)
                VALUES (?, ?, ?, 1, ?)
                """,
                (schedule_id, container_name, time_hhmm, iso(utcnow())),
            )
            return schedule_id, True

    def list_schedules(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM schedules"
        if enabled_only:
            query += " WHERE enabled=1"
        query += " ORDER BY time_hhmm, container_name"
        with self.connect() as db:
            return [dict(row) for row in db.execute(query).fetchall()]

    def disable_schedule(self, schedule_id: str) -> bool:
        with self.connect() as db:
            result = db.execute("UPDATE schedules SET enabled=0 WHERE id=? AND enabled=1", (schedule_id,))
            return result.rowcount == 1

    def disable_schedules_for_missing(self, existing_names: set[str]) -> list[str]:
        with self.connect() as db:
            rows = db.execute("SELECT id, container_name FROM schedules WHERE enabled=1").fetchall()
            missing = [dict(row) for row in rows if str(row["container_name"]) not in existing_names]
            for item in missing:
                db.execute("UPDATE schedules SET enabled=0, last_error='container removed' WHERE id=?", (item["id"],))
            return [str(item["container_name"]) for item in missing]

    def mark_schedule_attempt(self, schedule_id: str, attempted_at: datetime) -> None:
        stamp = iso(attempted_at)
        local_date = attempted_at.date().isoformat()
        with self.connect() as db:
            row = db.execute("SELECT last_attempt_date FROM schedules WHERE id=?", (schedule_id,)).fetchone()
            reset = not row or row["last_attempt_date"] != local_date
            if reset:
                db.execute(
                    "UPDATE schedules SET last_attempt_at=?, last_attempt_date=?, failure_count=0, last_error=NULL WHERE id=?",
                    (stamp, local_date, schedule_id),
                )
            else:
                db.execute(
                    "UPDATE schedules SET last_attempt_at=?, last_attempt_date=? WHERE id=?",
                    (stamp, local_date, schedule_id),
                )

    def mark_schedule_success(self, schedule_id: str, local_date: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE schedules SET last_run_date=?, failure_count=0, last_error=NULL WHERE id=?",
                (local_date, schedule_id),
            )

    def mark_schedule_failure(self, schedule_id: str, error: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE schedules SET failure_count=failure_count+1, last_error=? WHERE id=?",
                (error[:500], schedule_id),
            )
