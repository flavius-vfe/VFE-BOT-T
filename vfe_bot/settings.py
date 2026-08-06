from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _csv(name: str) -> set[str]:
    return {item.strip() for item in os.getenv(name, "").split(",") if item.strip()}


@dataclass(frozen=True, slots=True)
class Settings:
    telegram_token: str
    pairing_code: str
    database_path: str
    timezone: str
    bot_container_name: str
    protected_containers: set[str]
    notify_changes: bool
    poll_interval_seconds: int
    log_lines_default: int
    log_lines_max: int
    host_storage_path: str
    notify_health_changes: bool = True
    notify_created_removed: bool = True
    restart_loop_threshold: int = 3
    schedule_retry_minutes: int = 5
    schedule_max_attempts: int = 3
    diagnostics_audit_limit: int = 50

    @classmethod
    def load(cls) -> "Settings":
        token = os.getenv("TELEGRAM_TOKEN", "").strip()
        pairing = os.getenv("PAIRING_CODE", "").strip()
        if not token:
            raise RuntimeError("TELEGRAM_TOKEN is required")
        if not pairing:
            raise RuntimeError("PAIRING_CODE is required")

        bot_name = os.getenv("BOT_CONTAINER_NAME", "vfe-bot-t").strip() or "vfe-bot-t"
        protected = _csv("PROTECTED_CONTAINERS")
        protected.add(bot_name)

        return cls(
            telegram_token=token,
            pairing_code=pairing,
            database_path=os.getenv("DATABASE_PATH", "/data/vfe-bot.db"),
            timezone=os.getenv("TZ", "Europe/Bucharest"),
            bot_container_name=bot_name,
            protected_containers=protected,
            notify_changes=_bool("NOTIFY_CONTAINER_CHANGES", True),
            poll_interval_seconds=max(10, int(os.getenv("POLL_INTERVAL_SECONDS", "30"))),
            log_lines_default=max(1, int(os.getenv("LOG_LINES_DEFAULT", "50"))),
            log_lines_max=max(50, int(os.getenv("LOG_LINES_MAX", "300"))),
            host_storage_path=os.getenv("HOST_STORAGE_PATH", "/host-mnt/user"),
            notify_health_changes=_bool("NOTIFY_HEALTH_CHANGES", True),
            notify_created_removed=_bool("NOTIFY_CREATED_REMOVED", True),
            restart_loop_threshold=max(2, int(os.getenv("RESTART_LOOP_THRESHOLD", "3"))),
            schedule_retry_minutes=max(1, int(os.getenv("SCHEDULE_RETRY_MINUTES", "5"))),
            schedule_max_attempts=max(1, int(os.getenv("SCHEDULE_MAX_ATTEMPTS", "3"))),
            diagnostics_audit_limit=max(10, min(100, int(os.getenv("DIAGNOSTICS_AUDIT_LIMIT", "50")))),
        )
