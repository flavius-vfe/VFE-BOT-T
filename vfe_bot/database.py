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
                    created_at TEXT NOT NULL
                );
                """
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

    def add_schedule(self, container_name: str, time_hhmm: str) -> str:
        schedule_id = secrets.token_hex(3)
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO schedules(id, container_name, time_hhmm, enabled, created_at)
                VALUES (?, ?, ?, 1, ?)
                """,
                (schedule_id, container_name, time_hhmm, iso(utcnow())),
            )
        return schedule_id

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

    def mark_schedule_run(self, schedule_id: str, local_date: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE schedules SET last_run_date=? WHERE id=?", (local_date, schedule_id))
