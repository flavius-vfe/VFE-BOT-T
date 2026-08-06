from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import logging
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .database import Database
from .docker_service import DockerService, STATUS_ICONS, docker_error, human_bytes
from .settings import Settings
from .parser import parse_command
from .telegram_api import TelegramAPI


LOG = logging.getLogger("vfe-bot")
PAGE_SIZE = 16
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")

HELP = """<b>VFE Docker Bot</b>

<b>Automatic Docker discovery</b>
/containers — show every container
/server — server and Docker status
/info NAME — container details
/logs NAME [LINES] — recent logs
/stats NAME — CPU, RAM and network
/startc NAME — start container
/stop NAME — stop container
/restart NAME — restart container

<b>Schedules</b>
/schedule NAME HH:MM — daily restart
/schedules — list schedules
/unschedule ID — disable schedule

<b>Administration</b>
/audit [COUNT] — recent actions
/help — this message

You can also write: <code>restart plex</code>, <code>logs sonarr 100</code>, or <code>server</code>.
"""


class VFEBot:
    def __init__(self, settings: Settings, db: Database, docker: DockerService, telegram: TelegramAPI):
        self.settings = settings
        self.db = db
        self.docker = docker
        self.telegram = telegram
        self.offset = 0
        self.stop_event = asyncio.Event()
        self.known_states: dict[str, str] = {}

    async def start(self) -> None:
        me = await self.telegram.call("getMe")
        LOG.info("Connected as @%s", me.get("username"))
        await self.telegram.call(
            "setMyCommands",
            {
                "commands": [
                    {"command": "containers", "description": "List all Docker containers"},
                    {"command": "server", "description": "Server and Docker status"},
                    {"command": "logs", "description": "Show container logs"},
                    {"command": "restart", "description": "Restart a container"},
                    {"command": "schedules", "description": "List daily restarts"},
                    {"command": "audit", "description": "Show recent actions"},
                    {"command": "help", "description": "Show commands"},
                ]
            },
        )
        tasks = [
            asyncio.create_task(self.poll_forever(), name="telegram-poll"),
            asyncio.create_task(self.watch_containers(), name="container-watch"),
            asyncio.create_task(self.run_schedules(), name="schedules"),
            asyncio.create_task(self.heartbeat(), name="heartbeat"),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            self.stop_event.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def heartbeat(self) -> None:
        while not self.stop_event.is_set():
            try:
                with open("/data/heartbeat", "w", encoding="utf-8") as handle:
                    handle.write(datetime.now().isoformat())
            except OSError:
                pass
            await asyncio.sleep(20)

    async def poll_forever(self) -> None:
        while not self.stop_event.is_set():
            try:
                updates = await self.telegram.call(
                    "getUpdates",
                    {
                        "offset": self.offset,
                        "timeout": 30,
                        "allowed_updates": ["message", "callback_query"],
                    },
                )
                for update in updates:
                    self.offset = max(self.offset, int(update["update_id"]) + 1)
                    await self.handle_update(update)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("Telegram polling error: %s", exc)
                await asyncio.sleep(5)

    async def handle_update(self, update: dict[str, Any]) -> None:
        if callback := update.get("callback_query"):
            await self.handle_callback(callback)
            return
        message = update.get("message")
        if not message or not isinstance(message.get("text"), str):
            return
        await self.handle_message(message)

    async def handle_message(self, message: dict[str, Any]) -> None:
        user = message.get("from", {})
        chat = message.get("chat", {})
        user_id = int(user.get("id", 0))
        chat_id = int(chat.get("id", 0))
        text = message["text"].strip()

        command, args = parse_command(text)
        if command == "pair":
            await self.handle_pair(user_id, chat_id, chat.get("type"), user.get("username"), args)
            return

        if not self.db.is_owner(user_id, chat_id):
            owner = self.db.get_owner()
            if owner is None:
                await self.telegram.send(
                    chat_id,
                    "Bot is not paired. In a private chat send <code>/pair YOUR_CODE</code>.",
                )
            return

        try:
            await self.dispatch(user_id, chat_id, command, args)
        except Exception as exc:
            LOG.exception("Command failed")
            await self.telegram.send(chat_id, f"❌ {html.escape(docker_error(exc))}")

    async def handle_pair(
        self,
        user_id: int,
        chat_id: int,
        chat_type: str | None,
        username: str | None,
        args: list[str],
    ) -> None:
        if chat_type != "private":
            await self.telegram.send(chat_id, "Pairing is allowed only in a private chat.")
            return
        if self.db.get_owner() is not None:
            if self.db.is_owner(user_id, chat_id):
                await self.telegram.send(chat_id, "✅ This bot is already paired with you.")
            return
        submitted = args[0] if args else ""
        if not hmac.compare_digest(
            hashlib.sha256(submitted.encode()).digest(),
            hashlib.sha256(self.settings.pairing_code.encode()).digest(),
        ):
            await self.telegram.send(chat_id, "❌ Invalid pairing code.")
            return
        if self.db.set_owner(user_id, chat_id, username):
            self.db.audit(user_id, "pair", None, "success", username or "")
            await self.telegram.send(chat_id, "✅ Paired successfully.\n\n" + HELP)

    async def dispatch(self, user_id: int, chat_id: int, command: str, args: list[str]) -> None:
        if command in {"start", "help"}:
            await self.telegram.send(chat_id, HELP)
        elif command in {"containers", "list"}:
            await self.send_container_page(chat_id, 0)
        elif command in {"server", "status"}:
            await self.send_server(chat_id)
        elif command in {"info", "container"} and args:
            await self.send_container(chat_id, args[0])
        elif command == "logs" and args:
            lines = self.settings.log_lines_default
            if len(args) > 1 and args[1].isdigit():
                lines = min(self.settings.log_lines_max, max(1, int(args[1])))
            await self.send_logs(chat_id, args[0], lines)
        elif command == "stats" and args:
            await self.send_stats(chat_id, args[0])
        elif command in {"startc", "run"} and args:
            await self.request_action(user_id, chat_id, "start", args[0])
        elif command == "stop" and args:
            await self.request_action(user_id, chat_id, "stop", args[0])
        elif command == "restart" and args:
            await self.request_action(user_id, chat_id, "restart", args[0])
        elif command == "audit":
            limit = int(args[0]) if args and args[0].isdigit() else 20
            await self.send_audit(chat_id, limit)
        elif command == "schedule" and len(args) >= 2:
            await self.add_schedule(user_id, chat_id, args[0], args[1])
        elif command == "schedules":
            await self.send_schedules(chat_id)
        elif command == "unschedule" and args:
            disabled = await asyncio.to_thread(self.db.disable_schedule, args[0])
            await self.telegram.send(chat_id, "✅ Schedule disabled." if disabled else "Schedule not found.")
        else:
            await self.telegram.send(chat_id, "Unknown or incomplete command. Use /help")

    async def send_container_page(self, chat_id: int, page: int, message_id: int | None = None) -> None:
        containers = await asyncio.to_thread(self.docker.list_containers)
        total_pages = max(1, (len(containers) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        subset = containers[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        lines = [f"<b>Docker containers</b> — {len(containers)} found"]
        running = sum(1 for item in containers if item["status"] == "running")
        lines.append(f"Running: {running} · Stopped/other: {len(containers) - running}\n")
        for item in subset:
            icon = STATUS_ICONS.get(item["status"], "❔")
            shield = " 🔒" if item["protected"] else ""
            lines.append(f"{icon} <code>{html.escape(item['name'])}</code> — {html.escape(item['status'])}{shield}")

        rows: list[list[dict[str, str]]] = []
        for index in range(0, len(subset), 2):
            row = []
            for item in subset[index:index + 2]:
                row.append({"text": item["name"][:26], "callback_data": f"view:{item['id']}"})
            rows.append(row)
        nav = []
        if page > 0:
            nav.append({"text": "◀️", "callback_data": f"page:{page - 1}"})
        nav.append({"text": f"{page + 1}/{total_pages}", "callback_data": "noop"})
        if page + 1 < total_pages:
            nav.append({"text": "▶️", "callback_data": f"page:{page + 1}"})
        rows.append(nav)
        markup = {"inline_keyboard": rows}
        text = "\n".join(lines)
        if message_id:
            await self.telegram.edit(chat_id, message_id, text, markup)
        else:
            await self.telegram.send(chat_id, text, markup)

    async def send_container(self, chat_id: int, target: str, message_id: int | None = None) -> None:
        info = await asyncio.to_thread(self.docker.info, target)
        icon = STATUS_ICONS.get(info["status"], "❔")
        text = (
            f"{icon} <b>{html.escape(info['name'])}</b>\n"
            f"Status: <code>{html.escape(info['status'])}</code>\n"
            f"Image: <code>{html.escape(str(info['image']))}</code>\n"
            f"Restart policy: <code>{html.escape(str(info['restart_policy']))}</code>\n"
            f"Networks: <code>{html.escape(', '.join(info['networks']) or 'none')}</code>\n"
            f"Protected: {'yes' if info['protected'] else 'no'}"
        )
        rows = [
            [
                {"text": "📄 Logs", "callback_data": f"logs:{info['id']}"},
                {"text": "📊 Stats", "callback_data": f"stats:{info['id']}"},
            ]
        ]
        if not info["protected"]:
            rows.append(
                [
                    {"text": "▶️ Start", "callback_data": f"ask:start:{info['id']}"},
                    {"text": "⏹ Stop", "callback_data": f"ask:stop:{info['id']}"},
                    {"text": "🔄 Restart", "callback_data": f"ask:restart:{info['id']}"},
                ]
            )
        rows.append([{"text": "⬅️ Containers", "callback_data": "page:0"}])
        markup = {"inline_keyboard": rows}
        if message_id:
            await self.telegram.edit(chat_id, message_id, text, markup)
        else:
            await self.telegram.send(chat_id, text, markup)

    async def send_logs(self, chat_id: int, target: str, lines: int) -> None:
        info = await asyncio.to_thread(self.docker.info, target)
        output = await asyncio.to_thread(self.docker.logs, info["id"], lines)
        await self.telegram.send_long(
            chat_id,
            output[-12000:],
            header=f"<b>Logs: {html.escape(info['name'])}</b> (last {lines})\n",
        )

    async def send_stats(self, chat_id: int, target: str) -> None:
        stats = await asyncio.to_thread(self.docker.stats, target)
        text = (
            f"<b>Stats: {html.escape(stats['name'])}</b>\n"
            f"CPU: <code>{stats['cpu_percent']:.2f}%</code>\n"
            f"RAM: <code>{human_bytes(stats['memory_usage'])} / {human_bytes(stats['memory_limit'])}</code> "
            f"({stats['memory_percent']:.2f}%)\n"
            f"Network RX: <code>{human_bytes(stats['network_rx'])}</code>\n"
            f"Network TX: <code>{human_bytes(stats['network_tx'])}</code>"
        )
        await self.telegram.send(chat_id, text)

    async def send_server(self, chat_id: int) -> None:
        info = await asyncio.to_thread(self.docker.server_info)
        storage = info.get("storage")
        storage_line = "Storage mount: unavailable"
        if storage:
            percent = storage["used"] / storage["total"] * 100 if storage["total"] else 0
            storage_line = (
                f"Storage: <code>{human_bytes(storage['used'])} / {human_bytes(storage['total'])}</code> "
                f"({percent:.1f}%)"
            )
        text = (
            f"<b>Server: {html.escape(str(info['name']))}</b>\n"
            f"Docker: <code>{html.escape(str(info['docker_version']))}</code>\n"
            f"CPU threads: <code>{info['cpus']}</code>\n"
            f"RAM: <code>{human_bytes(info['memory_total'])}</code>\n"
            f"Containers: <code>{info['containers_running']} running / {info['containers']} total</code>\n"
            f"{storage_line}"
        )
        await self.telegram.send(chat_id, text)

    async def request_action(self, user_id: int, chat_id: int, action: str, target: str) -> None:
        info = await asyncio.to_thread(self.docker.info, target)
        if info["protected"]:
            await self.telegram.send(chat_id, f"🔒 Protected container: <code>{html.escape(info['name'])}</code>")
            return
        approval_id = await asyncio.to_thread(
            self.db.create_approval,
            user_id,
            action,
            info["name"],
            {"container_id": info["id"]},
        )
        markup = {
            "inline_keyboard": [[
                {"text": "✅ Confirm", "callback_data": f"yes:{approval_id}"},
                {"text": "❌ Cancel", "callback_data": f"no:{approval_id}"},
            ]]
        }
        await self.telegram.send(
            chat_id,
            f"Confirm <b>{html.escape(action)}</b> for <code>{html.escape(info['name'])}</code>?\n"
            "This confirmation expires in 3 minutes.",
            markup,
        )

    async def execute_approval(self, user_id: int, chat_id: int, approval_id: str) -> None:
        approval = await asyncio.to_thread(self.db.claim_approval, approval_id, user_id)
        if not approval:
            await self.telegram.send(chat_id, "Approval expired, cancelled, or already used.")
            return
        try:
            result = await asyncio.to_thread(
                self.docker.mutate,
                approval["action"],
                approval["payload"].get("container_id") or approval["target"],
            )
            await asyncio.to_thread(self.db.finish_approval, approval_id, "success")
            await asyncio.to_thread(
                self.db.audit,
                user_id,
                approval["action"],
                result["name"],
                "success",
                result["status"],
            )
            await self.telegram.send(
                chat_id,
                f"✅ <b>{html.escape(approval['action'])}</b> completed for "
                f"<code>{html.escape(result['name'])}</code>. Status: <code>{html.escape(result['status'])}</code>",
            )
        except Exception as exc:
            await asyncio.to_thread(self.db.finish_approval, approval_id, "failed")
            await asyncio.to_thread(
                self.db.audit,
                user_id,
                approval["action"],
                approval["target"],
                "failed",
                str(exc),
            )
            raise

    async def handle_callback(self, callback: dict[str, Any]) -> None:
        callback_id = str(callback.get("id", ""))
        user_id = int(callback.get("from", {}).get("id", 0))
        message = callback.get("message") or {}
        chat_id = int(message.get("chat", {}).get("id", 0))
        message_id = int(message.get("message_id", 0))
        data = str(callback.get("data", ""))
        if not self.db.is_owner(user_id, chat_id):
            await self.telegram.answer_callback(callback_id, "Not authorized")
            return
        await self.telegram.answer_callback(callback_id)
        try:
            if data == "noop":
                return
            if data.startswith("page:"):
                await self.send_container_page(chat_id, int(data.split(":", 1)[1]), message_id)
            elif data.startswith("view:"):
                await self.send_container(chat_id, data.split(":", 1)[1], message_id)
            elif data.startswith("logs:"):
                await self.send_logs(chat_id, data.split(":", 1)[1], self.settings.log_lines_default)
            elif data.startswith("stats:"):
                await self.send_stats(chat_id, data.split(":", 1)[1])
            elif data.startswith("ask:"):
                _, action, target = data.split(":", 2)
                await self.request_action(user_id, chat_id, action, target)
            elif data.startswith("yes:"):
                await self.execute_approval(user_id, chat_id, data.split(":", 1)[1])
            elif data.startswith("no:"):
                approval_id = data.split(":", 1)[1]
                denied = await asyncio.to_thread(self.db.deny_approval, approval_id, user_id)
                await self.telegram.send(chat_id, "Action cancelled." if denied else "Approval is no longer pending.")
        except Exception as exc:
            LOG.exception("Callback failed")
            await self.telegram.send(chat_id, f"❌ {html.escape(docker_error(exc))}")

    async def send_audit(self, chat_id: int, limit: int) -> None:
        entries = await asyncio.to_thread(self.db.list_audit, limit)
        if not entries:
            await self.telegram.send(chat_id, "No audit entries yet.")
            return
        lines = ["<b>Recent actions</b>"]
        for item in entries:
            stamp = item["created_at"].replace("T", " ")[:19]
            lines.append(
                f"<code>{html.escape(stamp)}</code> · {html.escape(item['status'])} · "
                f"{html.escape(item['action'])} {html.escape(item['target'] or '')}"
            )
        await self.telegram.send(chat_id, "\n".join(lines))

    async def add_schedule(self, user_id: int, chat_id: int, target: str, time_hhmm: str) -> None:
        if not TIME_RE.fullmatch(time_hhmm):
            await self.telegram.send(chat_id, "Use 24-hour time, for example: <code>/schedule plex 04:30</code>")
            return
        info = await asyncio.to_thread(self.docker.info, target)
        if info["protected"]:
            await self.telegram.send(chat_id, "Protected containers cannot be scheduled.")
            return
        schedule_id = await asyncio.to_thread(self.db.add_schedule, info["name"], time_hhmm)
        await asyncio.to_thread(self.db.audit, user_id, "schedule.add", info["name"], "success", time_hhmm)
        await self.telegram.send(
            chat_id,
            f"✅ Daily restart scheduled for <code>{html.escape(info['name'])}</code> at "
            f"<code>{time_hhmm}</code> ({html.escape(self.settings.timezone)}).\nID: <code>{schedule_id}</code>",
        )

    async def send_schedules(self, chat_id: int) -> None:
        schedules = await asyncio.to_thread(self.db.list_schedules, False)
        if not schedules:
            await self.telegram.send(chat_id, "No schedules configured.")
            return
        lines = [f"<b>Daily restart schedules</b> ({html.escape(self.settings.timezone)})"]
        for item in schedules:
            state = "enabled" if item["enabled"] else "disabled"
            lines.append(
                f"<code>{item['id']}</code> · {html.escape(item['time_hhmm'])} · "
                f"{html.escape(item['container_name'])} · {state}"
            )
        await self.telegram.send(chat_id, "\n".join(lines))

    async def watch_containers(self) -> None:
        owner_notified = False
        while not self.stop_event.is_set():
            try:
                containers = await asyncio.to_thread(self.docker.list_containers)
                current = {item["id"]: f"{item['name']}|{item['status']}" for item in containers}
                owner = await asyncio.to_thread(self.db.get_owner)
                if self.settings.notify_changes and owner and owner_notified:
                    for container_id, value in current.items():
                        old = self.known_states.get(container_id)
                        if old and old != value:
                            old_name, old_status = old.split("|", 1)
                            name, status = value.split("|", 1)
                            await self.telegram.send(
                                int(owner["chat_id"]),
                                f"🔔 <code>{html.escape(name or old_name)}</code>: "
                                f"{html.escape(old_status)} → <b>{html.escape(status)}</b>",
                                disable_notification=True,
                            )
                self.known_states = current
                owner_notified = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("Container watcher error: %s", exc)
            await asyncio.sleep(self.settings.poll_interval_seconds)

    async def run_schedules(self) -> None:
        zone = ZoneInfo(self.settings.timezone)
        while not self.stop_event.is_set():
            try:
                now = datetime.now(zone)
                hhmm = now.strftime("%H:%M")
                local_date = now.date().isoformat()
                schedules = await asyncio.to_thread(self.db.list_schedules, True)
                owner = await asyncio.to_thread(self.db.get_owner)
                for schedule in schedules:
                    if schedule["time_hhmm"] != hhmm or schedule["last_run_date"] == local_date:
                        continue
                    await asyncio.to_thread(self.db.mark_schedule_run, schedule["id"], local_date)
                    try:
                        result = await asyncio.to_thread(self.docker.mutate, "restart", schedule["container_name"])
                        await asyncio.to_thread(
                            self.db.audit,
                            int(owner["user_id"]) if owner else 0,
                            "scheduled.restart",
                            result["name"],
                            "success",
                            schedule["id"],
                        )
                        if owner:
                            await self.telegram.send(
                                int(owner["chat_id"]),
                                f"🕒 Scheduled restart completed: <code>{html.escape(result['name'])}</code>",
                                disable_notification=True,
                            )
                    except Exception as exc:
                        await asyncio.to_thread(
                            self.db.audit,
                            int(owner["user_id"]) if owner else 0,
                            "scheduled.restart",
                            schedule["container_name"],
                            "failed",
                            str(exc),
                        )
                        if owner:
                            await self.telegram.send(
                                int(owner["chat_id"]),
                                f"❌ Scheduled restart failed for <code>{html.escape(schedule['container_name'])}</code>: "
                                f"{html.escape(str(exc))}",
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("Scheduler error: %s", exc)
            await asyncio.sleep(20)
