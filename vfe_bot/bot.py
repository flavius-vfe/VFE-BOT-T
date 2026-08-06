from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import logging
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .database import Database
from .docker_service import DockerService, STATUS_ICONS, docker_error, human_bytes
from .parser import parse_command
from .settings import Settings
from .telegram_api import TelegramAPI


LOG = logging.getLogger("vfe-bot")
PAGE_SIZE = 12
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
SCHEDULE_TIMES = ("00:00", "02:00", "04:00", "06:00", "12:00", "18:00", "22:00", "23:30")

HELP = """<b>VFE Docker Bot</b>

You do not need to type container names. Open <b>Containers</b>, select a container, then tap an action.

<b>Menu commands</b>
/containers — choose and manage a container
/server — server and Docker status
/startc — choose a container to start
/stop — choose a container to stop
/restart — choose a container to restart
/logs — choose a container and log length
/stats — choose a container for live statistics
/schedule — choose a container and restart time
/export — choose a container and export YAML or XML
/schedules — view and disable schedules
/audit — recent actions
/help — open this menu

Typed container names are still supported, but optional.
"""

MODE_TITLES = {
    "manage": "Select a container",
    "info": "Select a container for details",
    "logs": "Select a container for logs",
    "stats": "Select a container for statistics",
    "start": "Select a container to start",
    "stop": "Select a container to stop",
    "restart": "Select a container to restart",
    "schedule": "Select a container to schedule",
    "export": "Select a container to export",
}


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
                    {"command": "containers", "description": "Select and manage a container"},
                    {"command": "server", "description": "Server and Docker status"},
                    {"command": "startc", "description": "Select a container to start"},
                    {"command": "stop", "description": "Select a container to stop"},
                    {"command": "restart", "description": "Select a container to restart"},
                    {"command": "logs", "description": "Select a container for logs"},
                    {"command": "stats", "description": "Select a container for statistics"},
                    {"command": "schedule", "description": "Select a container and time"},
                    {"command": "export", "description": "Export container YAML or XML"},
                    {"command": "schedules", "description": "View scheduled restarts"},
                    {"command": "help", "description": "Open the main menu"},
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
            await self.send_main_menu(chat_id, prefix="✅ Paired successfully.\n\n")

    async def dispatch(self, user_id: int, chat_id: int, command: str, args: list[str]) -> None:
        if command in {"start", "help", "menu"}:
            await self.send_main_menu(chat_id)
        elif command in {"containers", "list"}:
            await self.send_container_page(chat_id, 0, "manage")
        elif command in {"server", "status"}:
            await self.send_server(chat_id)
        elif command in {"info", "container"}:
            if args:
                await self.send_container(chat_id, args[0])
            else:
                await self.send_container_page(chat_id, 0, "info")
        elif command == "logs":
            if args:
                lines = self.settings.log_lines_default
                if len(args) > 1 and args[1].isdigit():
                    lines = min(self.settings.log_lines_max, max(1, int(args[1])))
                await self.send_logs(chat_id, args[0], lines)
            else:
                await self.send_container_page(chat_id, 0, "logs")
        elif command == "stats":
            if args:
                await self.send_stats(chat_id, args[0])
            else:
                await self.send_container_page(chat_id, 0, "stats")
        elif command in {"startc", "run"}:
            if args:
                await self.request_action(user_id, chat_id, "start", args[0])
            else:
                await self.send_container_page(chat_id, 0, "start")
        elif command == "stop":
            if args:
                await self.request_action(user_id, chat_id, "stop", args[0])
            else:
                await self.send_container_page(chat_id, 0, "stop")
        elif command == "restart":
            if args:
                await self.request_action(user_id, chat_id, "restart", args[0])
            else:
                await self.send_container_page(chat_id, 0, "restart")
        elif command == "export":
            if args:
                await self.send_export_menu(chat_id, args[0])
            else:
                await self.send_container_page(chat_id, 0, "export")
        elif command == "audit":
            limit = int(args[0]) if args and args[0].isdigit() else 20
            await self.send_audit(chat_id, limit)
        elif command == "schedule":
            if len(args) >= 2:
                await self.add_schedule(user_id, chat_id, args[0], args[1])
            elif args:
                await self.send_schedule_menu(chat_id, args[0])
            else:
                await self.send_container_page(chat_id, 0, "schedule")
        elif command == "schedules":
            await self.send_schedules(chat_id)
        elif command == "unschedule" and args:
            disabled = await asyncio.to_thread(self.db.disable_schedule, args[0])
            await self.telegram.send(chat_id, "✅ Schedule disabled." if disabled else "Schedule not found.")
        else:
            await self.send_main_menu(
                chat_id,
                prefix="I did not recognize that command. Select an option below instead.\n\n",
            )

    async def send_main_menu(
        self,
        chat_id: int,
        message_id: int | None = None,
        prefix: str = "",
    ) -> None:
        markup = {
            "inline_keyboard": [
                [
                    {"text": "🐳 Containers", "callback_data": "list:manage:0"},
                    {"text": "🖥 Server", "callback_data": "main:server"},
                ],
                [
                    {"text": "▶️ Start", "callback_data": "list:start:0"},
                    {"text": "⏹ Stop", "callback_data": "list:stop:0"},
                    {"text": "🔄 Restart", "callback_data": "list:restart:0"},
                ],
                [
                    {"text": "📄 Logs", "callback_data": "list:logs:0"},
                    {"text": "📊 Stats", "callback_data": "list:stats:0"},
                    {"text": "📦 Export", "callback_data": "list:export:0"},
                ],
                [
                    {"text": "🕒 Schedules", "callback_data": "main:schedules"},
                    {"text": "🧾 Audit", "callback_data": "main:audit"},
                ],
            ]
        }
        text = prefix + HELP
        if message_id:
            await self.telegram.edit(chat_id, message_id, text, markup)
        else:
            await self.telegram.send(chat_id, text, markup)

    async def send_container_page(
        self,
        chat_id: int,
        page: int,
        mode: str = "manage",
        message_id: int | None = None,
    ) -> None:
        if mode not in MODE_TITLES:
            mode = "manage"
        all_containers = await asyncio.to_thread(self.docker.list_containers)
        containers = all_containers
        if mode == "start":
            containers = [item for item in all_containers if item["status"] != "running" and not item["protected"]]
        elif mode in {"stop", "restart"}:
            containers = [item for item in all_containers if item["status"] == "running" and not item["protected"]]
        elif mode == "stats":
            containers = [item for item in all_containers if item["status"] == "running"]
        elif mode == "schedule":
            containers = [item for item in all_containers if not item["protected"]]
        total_pages = max(1, (len(containers) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        subset = containers[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        running = sum(1 for item in all_containers if item["status"] == "running")
        lines = [
            f"<b>{html.escape(MODE_TITLES[mode])}</b>",
            f"{len(containers)} available · {len(all_containers)} total · {running} running",
        ]
        if not containers:
            lines.append("No containers are currently available for this action.")
        if mode in {"start", "stop", "restart"}:
            lines.append("A confirmation button appears before any change.")

        rows: list[list[dict[str, str]]] = []
        for item in subset:
            icon = STATUS_ICONS.get(item["status"], "❔")
            shield = " 🔒" if item["protected"] else ""
            callback = f"view:{item['id']}" if mode in {"manage", "info"} else f"pick:{mode}:{item['id']}"
            rows.append(
                [{
                    "text": f"{icon} {item['name'][:42]}{shield}",
                    "callback_data": callback,
                }]
            )

        nav: list[dict[str, str]] = []
        if page > 0:
            nav.append({"text": "◀️", "callback_data": f"list:{mode}:{page - 1}"})
        nav.append({"text": f"{page + 1}/{total_pages}", "callback_data": "noop"})
        if page + 1 < total_pages:
            nav.append({"text": "▶️", "callback_data": f"list:{mode}:{page + 1}"})
        rows.append(nav)
        rows.append([{"text": "🏠 Main menu", "callback_data": "main:menu"}])
        markup = {"inline_keyboard": rows}
        text = "\n".join(lines)
        if message_id:
            await self.telegram.edit(chat_id, message_id, text, markup)
        else:
            await self.telegram.send(chat_id, text, markup)

    async def send_container(self, chat_id: int, target: str, message_id: int | None = None) -> None:
        info = await asyncio.to_thread(self.docker.info, target)
        icon = STATUS_ICONS.get(info["status"], "❔")
        health_line = f"\nHealth: <code>{html.escape(str(info['health']))}</code>" if info.get("health") else ""
        ports = ", ".join(info.get("ports") or []) or "none"
        text = (
            f"{icon} <b>{html.escape(info['name'])}</b>\n"
            f"Status: <code>{html.escape(info['status'])}</code>{health_line}\n"
            f"Image: <code>{html.escape(str(info['image']))}</code>\n"
            f"Restart policy: <code>{html.escape(str(info['restart_policy']))}</code>\n"
            f"Network: <code>{html.escape(str(info['network_mode']))}</code>\n"
            f"Ports: <code>{html.escape(ports)}</code>\n"
            f"Mounted paths: <code>{info.get('mount_count', 0)}</code>\n"
            f"Protected: {'yes' if info['protected'] else 'no'}"
        )
        rows: list[list[dict[str, str]]] = []
        read_row = [{"text": "📄 Logs", "callback_data": f"logmenu:{info['id']}"}]
        if info["status"] == "running":
            read_row.append({"text": "📊 Stats", "callback_data": f"stats:{info['id']}"})
        rows.append(read_row)
        profile_row: list[dict[str, str]] = []
        if not info["protected"]:
            profile_row.append({"text": "🕒 Schedule", "callback_data": f"sched:{info['id']}"})
        profile_row.append({"text": "📦 Export", "callback_data": f"export:{info['id']}"})
        rows.append(profile_row)
        if not info["protected"]:
            action_row: list[dict[str, str]] = []
            if info["status"] != "running":
                action_row.append({"text": "▶️ Start", "callback_data": f"ask:start:{info['id']}"})
            if info["status"] == "running":
                action_row.extend(
                    [
                        {"text": "⏹ Stop", "callback_data": f"ask:stop:{info['id']}"},
                        {"text": "🔄 Restart", "callback_data": f"ask:restart:{info['id']}"},
                    ]
                )
            if action_row:
                rows.insert(0, action_row)
        rows.append(
            [
                {"text": "🔃 Refresh", "callback_data": f"view:{info['id']}"},
                {"text": "⬅️ Containers", "callback_data": "list:manage:0"},
            ]
        )
        markup = {"inline_keyboard": rows}
        if message_id:
            await self.telegram.edit(chat_id, message_id, text, markup)
        else:
            await self.telegram.send(chat_id, text, markup)

    async def send_log_menu(self, chat_id: int, target: str, message_id: int | None = None) -> None:
        info = await asyncio.to_thread(self.docker.info, target)
        choices = sorted({50, 100, 200, 300, self.settings.log_lines_default, self.settings.log_lines_max})
        choices = [value for value in choices if 1 <= value <= self.settings.log_lines_max]
        rows: list[list[dict[str, str]]] = []
        for index in range(0, len(choices), 3):
            rows.append(
                [
                    {"text": f"{value} lines", "callback_data": f"logn:{value}:{info['id']}"}
                    for value in choices[index:index + 3]
                ]
            )
        rows.append([{"text": "⬅️ Container", "callback_data": f"view:{info['id']}"}])
        text = f"<b>Logs: {html.escape(info['name'])}</b>\nSelect how many recent lines to display."
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
        await self.telegram.send(
            chat_id,
            "Choose another action:",
            {"inline_keyboard": [[{"text": "⬅️ Container", "callback_data": f"view:{info['id']}"}]]},
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
        markup = {
            "inline_keyboard": [[
                {"text": "🔃 Refresh", "callback_data": f"stats:{stats['id']}"},
                {"text": "⬅️ Container", "callback_data": f"view:{stats['id']}"},
            ]]
        }
        await self.telegram.send(chat_id, text, markup)

    async def send_server(self, chat_id: int, message_id: int | None = None) -> None:
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
        markup = {"inline_keyboard": [[
            {"text": "🐳 Containers", "callback_data": "list:manage:0"},
            {"text": "🏠 Main menu", "callback_data": "main:menu"},
        ]]}
        if message_id:
            await self.telegram.edit(chat_id, message_id, text, markup)
        else:
            await self.telegram.send(chat_id, text, markup)

    async def send_schedule_menu(self, chat_id: int, target: str, message_id: int | None = None) -> None:
        info = await asyncio.to_thread(self.docker.info, target)
        if info["protected"]:
            await self.telegram.send(chat_id, "🔒 Protected containers cannot be scheduled.")
            return
        rows: list[list[dict[str, str]]] = []
        for index in range(0, len(SCHEDULE_TIMES), 2):
            rows.append(
                [
                    {
                        "text": time_value,
                        "callback_data": f"schedat:{time_value.replace(':', '')}:{info['id']}",
                    }
                    for time_value in SCHEDULE_TIMES[index:index + 2]
                ]
            )
        rows.append([{"text": "⬅️ Container", "callback_data": f"view:{info['id']}"}])
        text = (
            f"<b>Daily restart: {html.escape(info['name'])}</b>\n"
            f"Select a time in <code>{html.escape(self.settings.timezone)}</code>."
        )
        markup = {"inline_keyboard": rows}
        if message_id:
            await self.telegram.edit(chat_id, message_id, text, markup)
        else:
            await self.telegram.send(chat_id, text, markup)

    async def send_export_menu(self, chat_id: int, target: str, message_id: int | None = None) -> None:
        info = await asyncio.to_thread(self.docker.info, target)
        text = (
            f"<b>Export profile: {html.escape(info['name'])}</b>\n"
            "Choose a format. Sensitive environment values such as tokens and passwords are replaced with "
            "<code>&lt;redacted&gt;</code>."
        )
        markup = {
            "inline_keyboard": [
                [
                    {"text": "🟦 Compose YAML", "callback_data": f"exportfmt:yaml:{info['id']}"},
                    {"text": "🟧 Unraid XML", "callback_data": f"exportfmt:xml:{info['id']}"},
                ],
                [{"text": "⬅️ Container", "callback_data": f"view:{info['id']}"}],
            ]
        }
        if message_id:
            await self.telegram.edit(chat_id, message_id, text, markup)
        else:
            await self.telegram.send(chat_id, text, markup)

    async def export_profile(self, user_id: int, chat_id: int, target: str, file_format: str) -> None:
        info = await asyncio.to_thread(self.docker.info, target)
        filename, content, content_type = await asyncio.to_thread(
            self.docker.export_profile,
            info["id"],
            file_format,
        )
        await self.telegram.send_document(
            chat_id,
            filename,
            content,
            content_type,
            caption=(
                f"📦 <b>{html.escape(info['name'])}</b> profile exported as "
                f"<code>{html.escape(file_format.upper())}</code>. Sensitive environment values are redacted."
            ),
        )
        await asyncio.to_thread(self.db.audit, user_id, f"export.{file_format}", info["name"], "success", filename)

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
            "inline_keyboard": [
                [
                    {"text": "✅ Confirm", "callback_data": f"yes:{approval_id}"},
                    {"text": "❌ Cancel", "callback_data": f"no:{approval_id}"},
                ],
                [{"text": "⬅️ Container", "callback_data": f"view:{info['id']}"}],
            ]
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
                {"inline_keyboard": [[{"text": "Open container", "callback_data": f"view:{result['id']}"}]]},
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
            if data == "main:menu":
                await self.send_main_menu(chat_id, message_id)
            elif data == "main:server":
                await self.send_server(chat_id, message_id)
            elif data == "main:schedules":
                await self.send_schedules(chat_id, message_id)
            elif data == "main:audit":
                await self.send_audit(chat_id, 20, message_id)
            elif data.startswith("list:"):
                _, mode, page = data.split(":", 2)
                await self.send_container_page(chat_id, int(page), mode, message_id)
            elif data.startswith("view:"):
                await self.send_container(chat_id, data.split(":", 1)[1], message_id)
            elif data.startswith("pick:"):
                _, mode, target = data.split(":", 2)
                await self.handle_container_pick(user_id, chat_id, message_id, mode, target)
            elif data.startswith("logmenu:"):
                await self.send_log_menu(chat_id, data.split(":", 1)[1], message_id)
            elif data.startswith("logn:"):
                _, lines, target = data.split(":", 2)
                await self.send_logs(chat_id, target, int(lines))
            elif data.startswith("stats:"):
                await self.send_stats(chat_id, data.split(":", 1)[1])
            elif data.startswith("sched:"):
                await self.send_schedule_menu(chat_id, data.split(":", 1)[1], message_id)
            elif data.startswith("schedat:"):
                _, compact_time, target = data.split(":", 2)
                time_hhmm = f"{compact_time[:2]}:{compact_time[2:]}"
                await self.add_schedule(user_id, chat_id, target, time_hhmm)
            elif data.startswith("export:"):
                await self.send_export_menu(chat_id, data.split(":", 1)[1], message_id)
            elif data.startswith("exportfmt:"):
                _, file_format, target = data.split(":", 2)
                await self.export_profile(user_id, chat_id, target, file_format)
            elif data.startswith("ask:"):
                _, action, target = data.split(":", 2)
                await self.request_action(user_id, chat_id, action, target)
            elif data.startswith("yes:"):
                await self.execute_approval(user_id, chat_id, data.split(":", 1)[1])
            elif data.startswith("no:"):
                approval_id = data.split(":", 1)[1]
                denied = await asyncio.to_thread(self.db.deny_approval, approval_id, user_id)
                await self.telegram.send(chat_id, "Action cancelled." if denied else "Approval is no longer pending.")
            elif data.startswith("unsched:"):
                schedule_id = data.split(":", 1)[1]
                disabled = await asyncio.to_thread(self.db.disable_schedule, schedule_id)
                if disabled:
                    await asyncio.to_thread(self.db.audit, user_id, "schedule.disable", schedule_id, "success", "")
                await self.send_schedules(chat_id, message_id)
        except Exception as exc:
            LOG.exception("Callback failed")
            await self.telegram.send(chat_id, f"❌ {html.escape(docker_error(exc))}")

    async def handle_container_pick(
        self,
        user_id: int,
        chat_id: int,
        message_id: int,
        mode: str,
        target: str,
    ) -> None:
        if mode in {"manage", "info"}:
            await self.send_container(chat_id, target, message_id)
        elif mode == "logs":
            await self.send_log_menu(chat_id, target, message_id)
        elif mode == "stats":
            await self.send_stats(chat_id, target)
        elif mode in {"start", "stop", "restart"}:
            await self.request_action(user_id, chat_id, mode, target)
        elif mode == "schedule":
            await self.send_schedule_menu(chat_id, target, message_id)
        elif mode == "export":
            await self.send_export_menu(chat_id, target, message_id)
        else:
            await self.send_container(chat_id, target, message_id)

    async def send_audit(self, chat_id: int, limit: int, message_id: int | None = None) -> None:
        entries = await asyncio.to_thread(self.db.list_audit, limit)
        if not entries:
            text = "No audit entries yet."
        else:
            lines = ["<b>Recent actions</b>"]
            for item in entries:
                stamp = item["created_at"].replace("T", " ")[:19]
                lines.append(
                    f"<code>{html.escape(stamp)}</code> · {html.escape(item['status'])} · "
                    f"{html.escape(item['action'])} {html.escape(item['target'] or '')}"
                )
            text = "\n".join(lines)
        markup = {"inline_keyboard": [[{"text": "🏠 Main menu", "callback_data": "main:menu"}]]}
        if message_id:
            await self.telegram.edit(chat_id, message_id, text, markup)
        else:
            await self.telegram.send(chat_id, text, markup)

    async def add_schedule(self, user_id: int, chat_id: int, target: str, time_hhmm: str) -> None:
        if not TIME_RE.fullmatch(time_hhmm):
            await self.telegram.send(chat_id, "Use 24-hour time, for example: <code>04:30</code>")
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
            f"<code>{time_hhmm}</code> ({html.escape(self.settings.timezone)}).",
            {"inline_keyboard": [[
                {"text": "View schedules", "callback_data": "main:schedules"},
                {"text": "Container", "callback_data": f"view:{info['id']}"},
            ]]},
        )

    async def send_schedules(self, chat_id: int, message_id: int | None = None) -> None:
        schedules = await asyncio.to_thread(self.db.list_schedules, False)
        enabled = [item for item in schedules if item["enabled"]]
        if not schedules:
            text = "No schedules configured."
        else:
            lines = [f"<b>Daily restart schedules</b> ({html.escape(self.settings.timezone)})"]
            for item in schedules:
                state = "enabled" if item["enabled"] else "disabled"
                lines.append(
                    f"<code>{html.escape(item['time_hhmm'])}</code> · "
                    f"{html.escape(item['container_name'])} · {state}"
                )
            text = "\n".join(lines)
        rows = [
            [{"text": f"❌ Disable {item['time_hhmm']} · {item['container_name'][:24]}", "callback_data": f"unsched:{item['id']}"}]
            for item in enabled[:20]
        ]
        rows.append([{"text": "➕ Add schedule", "callback_data": "list:schedule:0"}])
        rows.append([{"text": "🏠 Main menu", "callback_data": "main:menu"}])
        markup = {"inline_keyboard": rows}
        if message_id:
            await self.telegram.edit(chat_id, message_id, text, markup)
        else:
            await self.telegram.send(chat_id, text, markup)

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
                                {"inline_keyboard": [[{"text": "Open", "callback_data": f"view:{container_id}"}]]},
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
                                {"inline_keyboard": [[{"text": "Open", "callback_data": f"view:{result['id']}"}]]},
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
