from __future__ import annotations

import asyncio
from pathlib import Path

from vfe_bot.bot import VFEBot
from vfe_bot.database import Database
from vfe_bot.settings import Settings


class FakeDocker:
    def list_containers(self):
        return [
            {"id": "run123", "name": "plex", "status": "running", "image": "plex", "protected": False},
            {"id": "stop123", "name": "sonarr", "status": "exited", "image": "sonarr", "protected": False},
            {"id": "bot123", "name": "vfe-bot-t", "status": "running", "image": "bot", "protected": True},
        ]

    def info(self, target: str):
        items = {item["id"]: item for item in self.list_containers()}
        item = items[target]
        return {
            **item,
            "health": None,
            "restart_policy": "unless-stopped",
            "network_mode": "bridge",
            "ports": [],
            "mount_count": 1,
        }


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.edits = []

    async def send(self, chat_id, text, reply_markup=None, disable_notification=False):
        self.messages.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})

    async def edit(self, chat_id, message_id, text, reply_markup=None):
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text, "reply_markup": reply_markup})


def settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_token="test",
        pairing_code="pair",
        database_path=str(tmp_path / "bot.db"),
        timezone="Europe/Bucharest",
        bot_container_name="vfe-bot-t",
        protected_containers={"vfe-bot-t"},
        notify_changes=False,
        poll_interval_seconds=30,
        log_lines_default=50,
        log_lines_max=300,
        host_storage_path=str(tmp_path),
    )


def test_action_commands_without_names_open_filtered_picker(tmp_path) -> None:
    db = Database(str(tmp_path / "bot.db"))
    db.initialize()
    telegram = FakeTelegram()
    bot = VFEBot(settings(tmp_path), db, FakeDocker(), telegram)  # type: ignore[arg-type]

    asyncio.run(bot.dispatch(1, 2, "restart", []))
    markup = telegram.messages[-1]["reply_markup"]
    buttons = [button for row in markup["inline_keyboard"] for button in row]
    labels = [button["text"] for button in buttons]
    callbacks = [button["callback_data"] for button in buttons]
    assert any("plex" in label for label in labels)
    assert not any("sonarr" in label for label in labels)
    assert "pick:restart:run123" in callbacks

    asyncio.run(bot.dispatch(1, 2, "startc", []))
    markup = telegram.messages[-1]["reply_markup"]
    buttons = [button for row in markup["inline_keyboard"] for button in row]
    labels = [button["text"] for button in buttons]
    assert any("sonarr" in label for label in labels)
    assert not any("plex" in label for label in labels)


def test_container_card_contains_all_button_actions(tmp_path) -> None:
    db = Database(str(tmp_path / "bot.db"))
    db.initialize()
    telegram = FakeTelegram()
    bot = VFEBot(settings(tmp_path), db, FakeDocker(), telegram)  # type: ignore[arg-type]

    asyncio.run(bot.send_container(2, "run123"))
    markup = telegram.messages[-1]["reply_markup"]
    callbacks = {
        button["callback_data"]
        for row in markup["inline_keyboard"]
        for button in row
    }
    assert "ask:stop:run123" in callbacks
    assert "ask:restart:run123" in callbacks
    assert "logmenu:run123" in callbacks
    assert "stats:run123" in callbacks
    assert "sched:run123" in callbacks
    assert "export:run123" in callbacks
