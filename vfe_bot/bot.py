from __future__ import annotations

import asyncio
import html
import io
import json
import logging
import re
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import __version__
from .database import Database
from .docker_service import DockerService, STATUS_ICONS, STATUS_LABELS, docker_error, human_bytes
from .parser import parse_command
from .settings import Settings
from .telegram_api import TelegramAPI


LOG = logging.getLogger("vfe-bot")
PAGE_SIZE = 10
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
SCHEDULE_TIMES = ("00:00", "02:00", "04:00", "06:00", "12:00", "18:00", "22:00", "23:30")
FILTERS = ("all", "running", "stopped", "unhealthy")

HELP = f"""<b>VFE Docker Bot v{__version__}</b>

Use buttons instead of typing container names. Open <b>Containers</b>, choose a container, then choose an action.

<b>Menus</b>
/containers — containers with state filters
/stacks — Compose projects and stack actions
/server — server and Docker status
/export — profile and backup exports
/schedules — restart schedules
/settings — bot configuration summary
/diagnostics — sanitized support bundle
/unpair — remove the paired Telegram owner
/help — this menu
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
        self.known_states: dict[str, dict[str, Any]] = {}
        self.docker_reachable = True
        self.restart_events: dict[str, list[datetime]] = {}
        self.restart_alerted_at: dict[str, datetime] = {}

    async def start(self) -> None:
        me = await self.telegram.call("getMe")
        LOG.info("Connected as @%s", me.get("username"))
        await self.telegram.call(
            "setMyCommands",
            {
                "commands": [
                    {"command": "containers", "description": "Containers, states and filters"},
                    {"command": "stacks", "description": "Compose projects and stack actions"},
                    {"command": "server", "description": "Server and Docker status"},
                    {"command": "startc", "description": "Select a container to start"},
                    {"command": "stop", "description": "Select a container to stop"},
                    {"command": "restart", "description": "Select a container to restart"},
                    {"command": "logs", "description": "Select a container for logs"},
                    {"command": "stats", "description": "Select a container for statistics"},
                    {"command": "schedule", "description": "Select a container and time"},
                    {"command": "export", "description": "Export profiles and backups"},
                    {"command": "schedules", "description": "View scheduled restarts"},
                    {"command": "settings", "description": "View bot settings"},
                    {"command": "diagnostics", "description": "Download diagnostics"},
                    {"command": "unpair", "description": "Remove paired owner"},
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
                    handle.write(datetime.now(UTC).isoformat())
            except OSError:
                pass
            await asyncio.sleep(20)

    async def poll_forever(self) -> None:
        while not self.stop_event.is_set():
            try:
                updates = await self.telegram.call(
                    "getUpdates",
                    {"offset": self.offset, "timeout": 30, "allowed_updates": ["message", "callback_query"]},
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
        command, args = parse_command(message["text"].strip())

        if command == "pair":
            await self.handle_pair(user_id, chat_id, chat.get("type"), user.get("username"), args)
            return
        if not self.db.is_owner(user_id, chat_id):
            if self.db.get_owner() is None:
                await self.telegram.send(chat_id, "Bot is not paired. In a private chat send <code>/pair YOUR_CODE</code>.")
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
        import hashlib
        import hmac

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
            await self.send_container_page(chat_id, 0, "manage", filter_name="all")
        elif command == "stacks":
            await self.send_stacks(chat_id)
        elif command in {"server", "status"}:
            await self.send_server(chat_id)
        elif command in {"info", "container"}:
            await self.send_container(chat_id, args[0]) if args else await self.send_container_page(chat_id, 0, "info")
        elif command == "logs":
            if args:
                lines = self.settings.log_lines_default
                if len(args) > 1 and args[1].isdigit():
                    lines = min(self.settings.log_lines_max, max(1, int(args[1])))
                await self.send_logs(chat_id, args[0], lines)
            else:
                await self.send_container_page(chat_id, 0, "logs")
        elif command == "stats":
            await self.send_stats(chat_id, args[0]) if args else await self.send_container_page(chat_id, 0, "stats")
        elif command in {"startc", "run"}:
            await self.request_action(user_id, chat_id, "start", args[0]) if args else await self.send_container_page(chat_id, 0, "start")
        elif command == "stop":
            await self.request_action(user_id, chat_id, "stop", args[0]) if args else await self.send_container_page(chat_id, 0, "stop")
        elif command == "restart":
            await self.request_action(user_id, chat_id, "restart", args[0]) if args else await self.send_container_page(chat_id, 0, "restart")
        elif command == "export":
            await self.send_export_menu(chat_id, args[0]) if args else await self.send_export_root(chat_id)
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
        elif command == "settings":
            await self.send_settings(chat_id)
        elif command == "diagnostics":
            await self.send_diagnostics(user_id, chat_id)
        elif command == "unpair":
            await self.request_unpair(user_id, chat_id)
        else:
            await self.send_main_menu(chat_id, prefix="I did not recognize that command. Select an option below.\n\n")

    async def send_main_menu(self, chat_id: int, message_id: int | None = None, prefix: str = "") -> None:
        markup = {
            "inline_keyboard": [
                [
                    {"text": "🐳 Containers", "callback_data": "list:manage:0:all"},
                    {"text": "🧩 Stacks", "callback_data": "main:stacks"},
                ],
                [
                    {"text": "▶️ Start", "callback_data": "list:start:0:all"},
                    {"text": "⏹ Stop", "callback_data": "list:stop:0:all"},
                    {"text": "🔄 Restart", "callback_data": "list:restart:0:all"},
                ],
                [
                    {"text": "🖥 Server", "callback_data": "main:server"},
                    {"text": "📦 Exports", "callback_data": "main:exports"},
                ],
                [
                    {"text": "🕒 Schedules", "callback_data": "main:schedules"},
                    {"text": "⚙️ Settings", "callback_data": "main:settings"},
                ],
                [
                    {"text": "🧾 Audit", "callback_data": "main:audit"},
                    {"text": "🩺 Diagnostics", "callback_data": "main:diagnostics"},
                ],
            ]
        }
        text = prefix + HELP
        if message_id:
            await self.telegram.edit(chat_id, message_id, text, markup)
        else:
            await self.telegram.send(chat_id, text, markup)

    @staticmethod
    def _filter_containers(items: list[dict[str, Any]], mode: str, filter_name: str) -> list[dict[str, Any]]:
        containers = items
        if mode == "start":
            containers = [item for item in containers if item["status"] != "running" and not item["protected"]]
        elif mode in {"stop", "restart"}:
            containers = [item for item in containers if item["status"] == "running" and not item["protected"]]
        elif mode == "stats":
            containers = [item for item in containers if item["status"] == "running"]
        elif mode == "schedule":
            containers = [item for item in containers if not item["protected"]]

        if filter_name == "running":
            containers = [item for item in containers if item["status"] == "running"]
        elif filter_name == "stopped":
            containers = [item for item in containers if item["status"] == "exited"]
        elif filter_name == "unhealthy":
            containers = [item for item in containers if item.get("health") == "unhealthy"]
        return containers

    async def send_container_page(
        self,
        chat_id: int,
        page: int,
        mode: str = "manage",
        message_id: int | None = None,
        filter_name: str = "all",
    ) -> None:
        if mode not in MODE_TITLES:
            mode = "manage"
        if filter_name not in FILTERS:
            filter_name = "all"
        all_containers = await asyncio.to_thread(self.docker.list_containers)
        containers = self._filter_containers(all_containers, mode, filter_name)
        total_pages = max(1, (len(containers) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        subset = containers[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        running = sum(1 for item in all_containers if item["status"] == "running")
        stopped = sum(1 for item in all_containers if item["status"] == "exited")
        unhealthy = sum(1 for item in all_containers if item.get("health") == "unhealthy")
        other = len(all_containers) - running - stopped
        summary = f"🟢 {running} running · 🔴 {stopped} stopped"
        if unhealthy:
            summary += f" · ❤️‍🩹 {unhealthy} unhealthy"
        if other:
            summary += f" · 🟡 {other} other"
        lines = [
            f"<b>{html.escape(MODE_TITLES[mode])}</b>",
            f"{len(containers)} shown · {len(all_containers)} total",
            summary,
        ]
        if filter_name != "all":
            lines.append(f"Filter: <code>{html.escape(filter_name)}</code>")
        if not containers:
            lines.append("No containers are available for this selection.")

        rows: list[list[dict[str, str]]] = []
        if mode == "manage":
            rows.append([
                {"text": "✅ All" if filter_name == "all" else "All", "callback_data": "list:manage:0:all"},
                {"text": "✅ Running" if filter_name == "running" else "Running", "callback_data": "list:manage:0:running"},
            ])
            rows.append([
                {"text": "✅ Stopped" if filter_name == "stopped" else "Stopped", "callback_data": "list:manage:0:stopped"},
                {"text": "✅ Unhealthy" if filter_name == "unhealthy" else "Unhealthy", "callback_data": "list:manage:0:unhealthy"},
            ])

        last_project = None
        for item in subset:
            if mode == "manage" and item.get("project") != last_project:
                last_project = item.get("project")
                if not str(last_project).startswith("Standalone:"):
                    rows.append([{"text": f"🧩 {str(last_project)[:42]}", "callback_data": f"stack:{item.get('project_token')}"}])
            status = str(item.get("status") or "unknown").lower()
            icon = STATUS_ICONS.get(status, "❔")
            status_label = STATUS_LABELS.get(status, status.upper())
            health = str(item.get("health") or "").lower()
            if status == "running" and health:
                status_label += f"/{health.upper()}"
            shield = " 🔒" if item.get("protected") else ""
            callback = f"view:{item['id']}" if mode in {"manage", "info"} else f"pick:{mode}:{item['id']}"
            name = str(item["name"])
            max_name_length = max(10, 40 - len(status_label))
            if len(name) > max_name_length:
                name = name[: max_name_length - 1] + "…"
            rows.append([{"text": f"{icon} {name} — {status_label}{shield}", "callback_data": callback}])

        nav: list[dict[str, str]] = []
        if page > 0:
            nav.append({"text": "◀️", "callback_data": f"list:{mode}:{page - 1}:{filter_name}"})
        nav.append({"text": f"{page + 1}/{total_pages}", "callback_data": "noop"})
        if page + 1 < total_pages:
            nav.append({"text": "▶️", "callback_data": f"list:{mode}:{page + 1}:{filter_name}"})
        rows.append(nav)
        rows.append([{"text": "🔃 Refresh", "callback_data": f"list:{mode}:{page}:{filter_name}"}, {"text": "🏠 Main", "callback_data": "main:menu"}])
        markup = {"inline_keyboard": rows}
        text = "\n".join(lines)
        if message_id:
            await self.telegram.edit(chat_id, message_id, text, markup)
        else:
            await self.telegram.send(chat_id, text, markup)

    async def send_container(self, chat_id: int, target: str, message_id: int | None = None) -> None:
        info = await asyncio.to_thread(self.docker.info, target)
        icon = STATUS_ICONS.get(info["status"], "❔")
        health = str(info.get("health") or "none")
        ports = ", ".join(info.get("ports") or []) or "none"
        metrics = ""
        if info["status"] == "running":
            try:
                stats = await asyncio.to_thread(self.docker.stats, info["id"])
                metrics = (
                    f"\nCPU: <code>{stats['cpu_percent']:.2f}%</code> · "
                    f"RAM: <code>{human_bytes(stats['memory_usage'])}</code> ({stats['memory_percent']:.1f}%)"
                )
            except Exception as exc:
                LOG.debug("Stats unavailable for %s: %s", info["name"], exc)
        alerts: list[str] = []
        if info.get("oom_killed"):
            alerts.append("OOM-killed")
        if info.get("exit_code") not in {None, 0}:
            alerts.append(f"exit {info['exit_code']}")
        if info.get("restart_count"):
            alerts.append(f"{info['restart_count']} restarts")
        alert_line = f"\nWarnings: <code>{html.escape(', '.join(alerts))}</code>" if alerts else ""
        text = (
            f"{icon} <b>{html.escape(info['name'])}</b>\n"
            f"State: <code>{html.escape(str(info['status']))}</code> · Health: <code>{html.escape(health)}</code>\n"
            f"Uptime: <code>{html.escape(str(info.get('uptime', '—')))}</code> · Restarts: <code>{info.get('restart_count', 0)}</code>\n"
            f"Stack: <code>{html.escape(str(info.get('project', 'Standalone')))}</code>\n"
            f"Image: <code>{html.escape(str(info['image']))}</code>\n"
            f"Restart policy: <code>{html.escape(str(info['restart_policy']))}</code>\n"
            f"Network: <code>{html.escape(str(info['network_mode']))}</code>\n"
            f"Ports: <code>{html.escape(ports)}</code>\n"
            f"Mounted paths: <code>{info.get('mount_count', 0)}</code> · Protected: <code>{'yes' if info['protected'] else 'no'}</code>"
            f"{metrics}{alert_line}"
        )
        rows: list[list[dict[str, str]]] = []
        if not info["protected"]:
            action_row: list[dict[str, str]] = []
            if info["status"] != "running":
                action_row.append({"text": "▶️ Start", "callback_data": f"ask:start:{info['id']}"})
            if info["status"] == "running":
                action_row.extend([
                    {"text": "⏹ Stop", "callback_data": f"ask:stop:{info['id']}"},
                    {"text": "🔄 Restart", "callback_data": f"ask:restart:{info['id']}"},
                ])
            if action_row:
                rows.append(action_row)
        read_row = [{"text": "📄 Logs", "callback_data": f"logmenu:{info['id']}"}]
        if info["status"] == "running":
            read_row.append({"text": "📊 Stats", "callback_data": f"stats:{info['id']}"})
        rows.append(read_row)
        profile_row: list[dict[str, str]] = []
        if not info["protected"]:
            profile_row.append({"text": "🕒 Schedule", "callback_data": f"sched:{info['id']}"})
        profile_row.append({"text": "📦 Export", "callback_data": f"export:{info['id']}"})
        rows.append(profile_row)
        if info.get("project_token"):
            rows.append([{"text": f"🧩 Open {str(info.get('project'))[:30]}", "callback_data": f"stack:{info['project_token']}"}])
        rows.append([
            {"text": "🔃 Refresh", "callback_data": f"view:{info['id']}"},
            {"text": "⬅️ Containers", "callback_data": "list:manage:0:all"},
        ])
        markup = {"inline_keyboard": rows}
        if message_id:
            await self.telegram.edit(chat_id, message_id, text, markup)
        else:
            await self.telegram.send(chat_id, text, markup)

    async def send_stacks(self, chat_id: int, message_id: int | None = None, page: int = 0) -> None:
        projects = await asyncio.to_thread(self.docker.list_projects)
        total_pages = max(1, (len(projects) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        subset = projects[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        lines = ["<b>Compose projects</b>", f"{len(projects)} project groups detected from Docker labels."]
        rows: list[list[dict[str, str]]] = []
        for project in subset:
            warning = f" · ❤️‍🩹 {project['unhealthy']}" if project["unhealthy"] else ""
            label = f"🧩 {project['name']} · 🟢{project['running']} 🔴{project['stopped']}{warning}"
            rows.append([{"text": label[:60], "callback_data": f"stack:{project['token']}"}])
        if not projects:
            lines.append("No containers were found.")
        nav: list[dict[str, str]] = []
        if page > 0:
            nav.append({"text": "◀️", "callback_data": f"stacklist:{page - 1}"})
        nav.append({"text": f"{page + 1}/{total_pages}", "callback_data": "noop"})
        if page + 1 < total_pages:
            nav.append({"text": "▶️", "callback_data": f"stacklist:{page + 1}"})
        rows.append(nav)
        rows.append([{"text": "📦 Export all", "callback_data": "exportall"}, {"text": "🏠 Main", "callback_data": "main:menu"}])
        markup = {"inline_keyboard": rows}
        if message_id:
            await self.telegram.edit(chat_id, message_id, "\n".join(lines), markup)
        else:
            await self.telegram.send(chat_id, "\n".join(lines), markup)

    async def send_stack(self, chat_id: int, token: str, message_id: int | None = None) -> None:
        project = await asyncio.to_thread(self.docker.project_info, token)
        lines = [
            f"<b>🧩 {html.escape(project['name'])}</b>",
            f"{len(project['containers'])} containers · 🟢 {project['running']} running · 🔴 {project['stopped']} stopped",
        ]
        for item in project["containers"][:30]:
            icon = STATUS_ICONS.get(item["status"], "❔")
            health = f"/{str(item['health']).upper()}" if item.get("health") else ""
            lines.append(f"{icon} {html.escape(item['name'])} — {html.escape(STATUS_LABELS.get(item['status'], item['status'].upper()) + health)}")
        if len(project["containers"]) > 30:
            lines.append(f"… and {len(project['containers']) - 30} more")
        rows: list[list[dict[str, str]]] = []
        for item in project["containers"][:12]:
            rows.append([{"text": f"{STATUS_ICONS.get(item['status'], '❔')} {item['name'][:45]}", "callback_data": f"view:{item['id']}"}])
        if not project["protected"]:
            rows.append([
                {"text": "▶️ Start stopped", "callback_data": f"askstack:start:{project['token']}"},
                {"text": "⏹ Stop running", "callback_data": f"askstack:stop:{project['token']}"},
            ])
            rows.append([{"text": "🔄 Restart running", "callback_data": f"askstack:restart:{project['token']}"}])
        rows.append([{"text": "📦 Export stack ZIP", "callback_data": f"exportstack:{project['token']}"}])
        rows.append([{"text": "⬅️ Stacks", "callback_data": "main:stacks"}, {"text": "🏠 Main", "callback_data": "main:menu"}])
        markup = {"inline_keyboard": rows}
        if message_id:
            await self.telegram.edit(chat_id, message_id, "\n".join(lines), markup)
        else:
            await self.telegram.send(chat_id, "\n".join(lines), markup)

    async def send_log_menu(self, chat_id: int, target: str, message_id: int | None = None) -> None:
        info = await asyncio.to_thread(self.docker.info, target)
        choices = sorted({50, 100, 200, 300, self.settings.log_lines_default, self.settings.log_lines_max})
        choices = [value for value in choices if 1 <= value <= self.settings.log_lines_max]
        rows = [
            [{"text": f"{value} lines", "callback_data": f"logn:{value}:{info['id']}"} for value in choices[index:index + 3]]
            for index in range(0, len(choices), 3)
        ]
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
        await self.telegram.send_long(chat_id, output[-12000:], header=f"<b>Logs: {html.escape(info['name'])}</b> (last {lines})\n")
        await self.telegram.send(chat_id, "Choose another action:", {"inline_keyboard": [[{"text": "⬅️ Container", "callback_data": f"view:{info['id']}"}]]})

    async def send_stats(self, chat_id: int, target: str) -> None:
        stats = await asyncio.to_thread(self.docker.stats, target)
        text = (
            f"<b>Stats: {html.escape(stats['name'])}</b>\n"
            f"CPU: <code>{stats['cpu_percent']:.2f}%</code>\n"
            f"RAM: <code>{human_bytes(stats['memory_usage'])} / {human_bytes(stats['memory_limit'])}</code> ({stats['memory_percent']:.2f}%)\n"
            f"Network RX: <code>{human_bytes(stats['network_rx'])}</code>\n"
            f"Network TX: <code>{human_bytes(stats['network_tx'])}</code>"
        )
        markup = {"inline_keyboard": [[
            {"text": "🔃 Refresh", "callback_data": f"stats:{stats['id']}"},
            {"text": "⬅️ Container", "callback_data": f"view:{stats['id']}"},
        ]]}
        await self.telegram.send(chat_id, text, markup)

    async def send_server(self, chat_id: int, message_id: int | None = None) -> None:
        info = await asyncio.to_thread(self.docker.server_info)
        storage = info.get("storage")
        storage_line = "Storage mount: unavailable"
        if storage:
            percent = storage["used"] / storage["total"] * 100 if storage["total"] else 0
            storage_line = f"Storage: <code>{human_bytes(storage['used'])} / {human_bytes(storage['total'])}</code> ({percent:.1f}%)"
        text = (
            f"<b>Server: {html.escape(str(info['name']))}</b>\n"
            f"Bot: <code>v{__version__}</code> · Docker: <code>{html.escape(str(info['docker_version']))}</code>\n"
            f"CPU threads: <code>{info['cpus']}</code> · RAM: <code>{human_bytes(info['memory_total'])}</code>\n"
            f"Containers: <code>{info['containers_running']} running / {info['containers']} total</code>\n"
            f"{storage_line}"
        )
        markup = {"inline_keyboard": [[
            {"text": "🐳 Containers", "callback_data": "list:manage:0:all"},
            {"text": "🔃 Refresh", "callback_data": "main:server"},
            {"text": "🏠 Main", "callback_data": "main:menu"},
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
        rows = [
            [{"text": time_value, "callback_data": f"schedat:{time_value.replace(':', '')}:{info['id']}"} for time_value in SCHEDULE_TIMES[index:index + 2]]
            for index in range(0, len(SCHEDULE_TIMES), 2)
        ]
        rows.append([{"text": "⬅️ Container", "callback_data": f"view:{info['id']}"}])
        text = f"<b>Daily restart: {html.escape(info['name'])}</b>\nSelect a time in <code>{html.escape(self.settings.timezone)}</code>."
        markup = {"inline_keyboard": rows}
        if message_id:
            await self.telegram.edit(chat_id, message_id, text, markup)
        else:
            await self.telegram.send(chat_id, text, markup)

    async def send_export_root(self, chat_id: int, message_id: int | None = None) -> None:
        text = (
            "<b>Profile exports and backups</b>\n"
            "Exports redact password-, token-, auth-, secret- and credential-like values. "
            "Review every file before restoring it."
        )
        markup = {"inline_keyboard": [
            [{"text": "📄 One container", "callback_data": "list:export:0:all"}],
            [{"text": "🧩 One stack", "callback_data": "main:stacks"}],
            [{"text": "📦 Export all ZIP", "callback_data": "exportall"}],
            [{"text": "🏠 Main menu", "callback_data": "main:menu"}],
        ]}
        if message_id:
            await self.telegram.edit(chat_id, message_id, text, markup)
        else:
            await self.telegram.send(chat_id, text, markup)

    async def send_export_menu(self, chat_id: int, target: str, message_id: int | None = None) -> None:
        info = await asyncio.to_thread(self.docker.info, target)
        text = (
            f"<b>Export profile: {html.escape(info['name'])}</b>\n"
            "Choose Compose YAML or Unraid XML. Sensitive values are replaced with <code>&lt;redacted&gt;</code>."
        )
        markup = {"inline_keyboard": [
            [
                {"text": "🟦 Compose YAML", "callback_data": f"exportfmt:yaml:{info['id']}"},
                {"text": "🟧 Unraid XML", "callback_data": f"exportfmt:xml:{info['id']}"},
            ],
            [{"text": "⬅️ Container", "callback_data": f"view:{info['id']}"}],
        ]}
        if message_id:
            await self.telegram.edit(chat_id, message_id, text, markup)
        else:
            await self.telegram.send(chat_id, text, markup)

    async def export_profile(self, user_id: int, chat_id: int, target: str, file_format: str) -> None:
        info = await asyncio.to_thread(self.docker.info, target)
        filename, content, content_type = await asyncio.to_thread(self.docker.export_profile, info["id"], file_format)
        await self.telegram.send_document(
            chat_id, filename, content, content_type,
            caption=f"📦 <b>{html.escape(info['name'])}</b> exported as <code>{html.escape(file_format.upper())}</code>. Sensitive values are redacted.",
        )
        await asyncio.to_thread(self.db.audit, user_id, f"export.{file_format}", info["name"], "success", filename)

    async def export_stack(self, user_id: int, chat_id: int, token: str) -> None:
        project = await asyncio.to_thread(self.docker.project_info, token)
        filename, content, content_type = await asyncio.to_thread(self.docker.export_project, token)
        await self.telegram.send_document(chat_id, filename, content, content_type, caption=f"📦 Stack export: <b>{html.escape(project['name'])}</b>. Sensitive values are redacted.")
        await asyncio.to_thread(self.db.audit, user_id, "export.stack", project["name"], "success", filename)

    async def export_all(self, user_id: int, chat_id: int) -> None:
        filename, content, content_type = await asyncio.to_thread(self.docker.export_all)
        await self.telegram.send_document(chat_id, filename, content, content_type, caption="📦 All container profiles exported. Sensitive values are redacted.")
        await asyncio.to_thread(self.db.audit, user_id, "export.all", None, "success", filename)

    async def request_action(self, user_id: int, chat_id: int, action: str, target: str) -> None:
        info = await asyncio.to_thread(self.docker.info, target)
        if info["protected"]:
            await self.telegram.send(chat_id, f"🔒 Protected container: <code>{html.escape(info['name'])}</code>")
            return
        approval_id = await asyncio.to_thread(self.db.create_approval, user_id, action, info["name"], {"container_id": info["id"]})
        markup = {"inline_keyboard": [
            [{"text": "✅ Confirm", "callback_data": f"yes:{approval_id}"}, {"text": "❌ Cancel", "callback_data": f"no:{approval_id}"}],
            [{"text": "⬅️ Container", "callback_data": f"view:{info['id']}"}],
        ]}
        await self.telegram.send(chat_id, f"Confirm <b>{html.escape(action)}</b> for <code>{html.escape(info['name'])}</code>?\nThis confirmation expires in 3 minutes.", markup)

    async def request_stack_action(self, user_id: int, chat_id: int, action: str, token: str) -> None:
        project = await asyncio.to_thread(self.docker.project_info, token)
        approval_id = await asyncio.to_thread(
            self.db.create_approval, user_id, f"project.{action}", project["name"], {"project_token": token}
        )
        markup = {"inline_keyboard": [[
            {"text": "✅ Confirm stack action", "callback_data": f"yes:{approval_id}"},
            {"text": "❌ Cancel", "callback_data": f"no:{approval_id}"},
        ], [{"text": "⬅️ Stack", "callback_data": f"stack:{token}"}]]}
        await self.telegram.send(
            chat_id,
            f"Confirm <b>{html.escape(action)}</b> for the eligible containers in stack <code>{html.escape(project['name'])}</code>?",
            markup,
        )

    async def request_unpair(self, user_id: int, chat_id: int) -> None:
        approval_id = await asyncio.to_thread(self.db.create_approval, user_id, "unpair", "telegram-owner", {})
        await self.telegram.send(
            chat_id,
            "⚠️ Unpair this Telegram account? The bot will stop accepting management commands until paired again.",
            {"inline_keyboard": [[
                {"text": "✅ Unpair", "callback_data": f"yes:{approval_id}"},
                {"text": "❌ Keep paired", "callback_data": f"no:{approval_id}"},
            ]]},
        )

    async def execute_approval(self, user_id: int, chat_id: int, approval_id: str) -> None:
        approval = await asyncio.to_thread(self.db.claim_approval, approval_id, user_id)
        if not approval:
            await self.telegram.send(chat_id, "Approval expired, cancelled, or already used.")
            return
        try:
            if approval["action"] == "unpair":
                cleared = await asyncio.to_thread(self.db.clear_owner, user_id)
                await asyncio.to_thread(self.db.finish_approval, approval_id, "success")
                if cleared:
                    await self.telegram.send(chat_id, "✅ Bot unpaired. Use <code>/pair YOUR_CODE</code> to pair again.")
                return
            if approval["action"].startswith("project."):
                action = approval["action"].split(".", 1)[1]
                result = await asyncio.to_thread(self.docker.mutate_project, action, approval["payload"]["project_token"])
                await asyncio.to_thread(self.db.finish_approval, approval_id, "success")
                detail = f"{len(result['results'])} changed, {len(result.get('errors', []))} failed"
                status = "partial" if result.get("errors") else "success"
                await asyncio.to_thread(self.db.audit, user_id, approval["action"], result["project"], status, detail)
                error_line = ""
                if result.get("errors"):
                    failed_names = ", ".join(item["name"] for item in result["errors"][:8])
                    error_line = f"\nFailed: <code>{html.escape(failed_names)}</code>"
                await self.telegram.send(
                    chat_id,
                    f"{'⚠️' if result.get('errors') else '✅'} Stack <b>{html.escape(action)}</b> completed for "
                    f"<code>{html.escape(result['project'])}</code>. Changed: <code>{len(result['results'])}</code> · "
                    f"Failed: <code>{len(result.get('errors', []))}</code>{error_line}",
                    {"inline_keyboard": [[{"text": "Open stack", "callback_data": f"stack:{approval['payload']['project_token']}"}]]},
                )
                return
            result = await asyncio.to_thread(self.docker.mutate, approval["action"], approval["payload"].get("container_id") or approval["target"])
            await asyncio.to_thread(self.db.finish_approval, approval_id, "success")
            await asyncio.to_thread(self.db.audit, user_id, approval["action"], result["name"], "success", result["status"])
            await self.telegram.send(
                chat_id,
                f"✅ <b>{html.escape(approval['action'])}</b> completed for <code>{html.escape(result['name'])}</code>. Status: <code>{html.escape(result['status'])}</code>",
                {"inline_keyboard": [[{"text": "Open container", "callback_data": f"view:{result['id']}"}]]},
            )
        except Exception as exc:
            await asyncio.to_thread(self.db.finish_approval, approval_id, "failed")
            await asyncio.to_thread(self.db.audit, user_id, approval["action"], approval["target"], "failed", str(exc))
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
            elif data == "main:stacks":
                await self.send_stacks(chat_id, message_id)
            elif data == "main:exports":
                await self.send_export_root(chat_id, message_id)
            elif data == "main:schedules":
                await self.send_schedules(chat_id, message_id)
            elif data == "main:audit":
                await self.send_audit(chat_id, 20, message_id)
            elif data == "main:settings":
                await self.send_settings(chat_id, message_id)
            elif data == "main:diagnostics":
                await self.send_diagnostics(user_id, chat_id)
            elif data.startswith("list:"):
                parts = data.split(":")
                _, mode, page = parts[:3]
                filter_name = parts[3] if len(parts) > 3 else "all"
                await self.send_container_page(chat_id, int(page), mode, message_id, filter_name)
            elif data.startswith("view:"):
                await self.send_container(chat_id, data.split(":", 1)[1], message_id)
            elif data.startswith("pick:"):
                _, mode, target = data.split(":", 2)
                await self.handle_container_pick(user_id, chat_id, message_id, mode, target)
            elif data.startswith("stacklist:"):
                await self.send_stacks(chat_id, message_id, int(data.split(":", 1)[1]))
            elif data.startswith("stack:"):
                await self.send_stack(chat_id, data.split(":", 1)[1], message_id)
            elif data.startswith("askstack:"):
                _, action, token = data.split(":", 2)
                await self.request_stack_action(user_id, chat_id, action, token)
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
                await self.add_schedule(user_id, chat_id, target, f"{compact_time[:2]}:{compact_time[2:]}")
            elif data.startswith("export:"):
                await self.send_export_menu(chat_id, data.split(":", 1)[1], message_id)
            elif data.startswith("exportfmt:"):
                _, file_format, target = data.split(":", 2)
                await self.export_profile(user_id, chat_id, target, file_format)
            elif data.startswith("exportstack:"):
                await self.export_stack(user_id, chat_id, data.split(":", 1)[1])
            elif data == "exportall":
                await self.export_all(user_id, chat_id)
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
            elif data == "unpair":
                await self.request_unpair(user_id, chat_id)
        except Exception as exc:
            LOG.exception("Callback failed")
            await self.telegram.send(chat_id, f"❌ {html.escape(docker_error(exc))}")

    async def handle_container_pick(self, user_id: int, chat_id: int, message_id: int, mode: str, target: str) -> None:
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
        lines = ["<b>Recent actions</b>"]
        if not entries:
            lines.append("No audit entries yet.")
        for item in entries:
            stamp = item["created_at"].replace("T", " ")[:19]
            lines.append(f"<code>{html.escape(stamp)}</code> · {html.escape(item['status'])} · {html.escape(item['action'])} {html.escape(item['target'] or '')}")
        markup = {"inline_keyboard": [[{"text": "🏠 Main menu", "callback_data": "main:menu"}]]}
        if message_id:
            await self.telegram.edit(chat_id, message_id, "\n".join(lines), markup)
        else:
            await self.telegram.send(chat_id, "\n".join(lines), markup)

    async def add_schedule(self, user_id: int, chat_id: int, target: str, time_hhmm: str) -> None:
        if not TIME_RE.fullmatch(time_hhmm):
            await self.telegram.send(chat_id, "Use 24-hour time, for example: <code>04:30</code>")
            return
        info = await asyncio.to_thread(self.docker.info, target)
        if info["protected"]:
            await self.telegram.send(chat_id, "Protected containers cannot be scheduled.")
            return
        schedule_id, created = await asyncio.to_thread(self.db.add_schedule, info["name"], time_hhmm)
        detail = time_hhmm if created else f"re-enabled {time_hhmm}"
        await asyncio.to_thread(self.db.audit, user_id, "schedule.add", info["name"], "success", detail)
        await self.telegram.send(
            chat_id,
            f"✅ Daily restart {'created' if created else 'already existed and was enabled'} for <code>{html.escape(info['name'])}</code> at <code>{time_hhmm}</code> ({html.escape(self.settings.timezone)}).",
            {"inline_keyboard": [[{"text": "View schedules", "callback_data": "main:schedules"}, {"text": "Container", "callback_data": f"view:{info['id']}"}]]},
        )

    async def send_schedules(self, chat_id: int, message_id: int | None = None) -> None:
        schedules = await asyncio.to_thread(self.db.list_schedules, False)
        enabled = [item for item in schedules if item["enabled"]]
        lines = [f"<b>Daily restart schedules</b> ({html.escape(self.settings.timezone)})"]
        if not schedules:
            lines.append("No schedules configured.")
        for item in schedules:
            state = "enabled" if item["enabled"] else "disabled"
            failures = f" · failures {item.get('failure_count', 0)}" if item.get("failure_count") else ""
            lines.append(f"<code>{html.escape(item['time_hhmm'])}</code> · {html.escape(item['container_name'])} · {state}{failures}")
        rows = [[{"text": f"❌ Disable {item['time_hhmm']} · {item['container_name'][:24]}", "callback_data": f"unsched:{item['id']}"}] for item in enabled[:20]]
        rows.append([{"text": "➕ Add schedule", "callback_data": "list:schedule:0:all"}])
        rows.append([{"text": "🏠 Main menu", "callback_data": "main:menu"}])
        markup = {"inline_keyboard": rows}
        if message_id:
            await self.telegram.edit(chat_id, message_id, "\n".join(lines), markup)
        else:
            await self.telegram.send(chat_id, "\n".join(lines), markup)

    async def send_settings(self, chat_id: int, message_id: int | None = None) -> None:
        protected = ", ".join(sorted(self.settings.protected_containers)) or "none"
        text = (
            f"<b>Settings · v{__version__}</b>\n"
            f"Timezone: <code>{html.escape(self.settings.timezone)}</code>\n"
            f"Poll interval: <code>{self.settings.poll_interval_seconds}s</code>\n"
            f"State notifications: <code>{'on' if self.settings.notify_changes else 'off'}</code>\n"
            f"Health notifications: <code>{'on' if self.settings.notify_health_changes else 'off'}</code>\n"
            f"Created/removed notifications: <code>{'on' if self.settings.notify_created_removed else 'off'}</code>\n"
            f"Restart-loop threshold: <code>{self.settings.restart_loop_threshold}</code>\n"
            f"Schedule retries: <code>{self.settings.schedule_max_attempts}</code> attempts, {self.settings.schedule_retry_minutes}m apart\n"
            f"Protected: <code>{html.escape(protected)}</code>"
        )
        markup = {"inline_keyboard": [
            [{"text": "🩺 Diagnostics", "callback_data": "main:diagnostics"}],
            [{"text": "🔓 Unpair bot", "callback_data": "unpair"}],
            [{"text": "🏠 Main menu", "callback_data": "main:menu"}],
        ]}
        if message_id:
            await self.telegram.edit(chat_id, message_id, text, markup)
        else:
            await self.telegram.send(chat_id, text, markup)

    async def send_diagnostics(self, user_id: int, chat_id: int) -> None:
        diagnostics = await asyncio.to_thread(self.docker.diagnostics)
        diagnostics["bot"] = {
            "version": __version__,
            "timezone": self.settings.timezone,
            "poll_interval_seconds": self.settings.poll_interval_seconds,
            "notify_changes": self.settings.notify_changes,
            "notify_health_changes": self.settings.notify_health_changes,
            "protected_containers": sorted(self.settings.protected_containers),
        }
        schedules = await asyncio.to_thread(self.db.list_schedules, False)
        audit = await asyncio.to_thread(self.db.list_audit, self.settings.diagnostics_audit_limit)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("diagnostics.json", json.dumps(diagnostics, indent=2, ensure_ascii=False, default=str))
            archive.writestr("schedules.json", json.dumps(schedules, indent=2, ensure_ascii=False, default=str))
            archive.writestr("audit.json", json.dumps(audit, indent=2, ensure_ascii=False, default=str))
            archive.writestr("README.txt", "Sanitized VFE Docker Bot diagnostics. Telegram token and pairing code are not included.\n")
        filename = f"vfe-bot-diagnostics-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.zip"
        await self.telegram.send_document(chat_id, filename, buffer.getvalue(), "application/zip", caption="🩺 Sanitized diagnostic bundle. Tokens and pairing codes are excluded.")
        await asyncio.to_thread(self.db.audit, user_id, "diagnostics.export", None, "success", filename)

    async def watch_containers(self) -> None:
        initialized = False
        while not self.stop_event.is_set():
            try:
                containers = await asyncio.to_thread(self.docker.list_containers)
                current = {item["id"]: item for item in containers}
                owner = await asyncio.to_thread(self.db.get_owner)
                if not self.docker_reachable and owner:
                    await self.telegram.send(int(owner["chat_id"]), "✅ Docker connection recovered.")
                self.docker_reachable = True

                if owner and initialized:
                    chat_id = int(owner["chat_id"])
                    if self.settings.notify_created_removed:
                        for container_id in current.keys() - self.known_states.keys():
                            item = current[container_id]
                            await self.telegram.send(chat_id, f"➕ Container created: <code>{html.escape(item['name'])}</code>", {"inline_keyboard": [[{"text": "Open", "callback_data": f"view:{container_id}"}]]}, disable_notification=True)
                        for container_id in self.known_states.keys() - current.keys():
                            item = self.known_states[container_id]
                            await self.telegram.send(chat_id, f"➖ Container removed: <code>{html.escape(item['name'])}</code>", disable_notification=True)
                    for container_id, item in current.items():
                        old = self.known_states.get(container_id)
                        if not old:
                            continue
                        if self.settings.notify_changes and old.get("status") != item.get("status"):
                            await self.telegram.send(
                                chat_id,
                                f"🔔 <code>{html.escape(item['name'])}</code>: {html.escape(str(old.get('status')))} → <b>{html.escape(str(item.get('status')))}</b>",
                                {"inline_keyboard": [[{"text": "Open", "callback_data": f"view:{container_id}"}]]},
                                disable_notification=True,
                            )
                        if self.settings.notify_health_changes and old.get("health") != item.get("health") and item.get("health"):
                            icon = "✅" if item.get("health") == "healthy" else "⚠️"
                            await self.telegram.send(
                                chat_id,
                                f"{icon} <code>{html.escape(item['name'])}</code> health: <b>{html.escape(str(item.get('health')))}</b>",
                                {"inline_keyboard": [[{"text": "Logs", "callback_data": f"logmenu:{container_id}"}, {"text": "Open", "callback_data": f"view:{container_id}"}]]},
                                disable_notification=item.get("health") == "healthy",
                            )
                        restart_delta = max(0, int(item.get("restart_count", 0)) - int(old.get("restart_count", 0)))
                        event_now = datetime.now(UTC)
                        events = self.restart_events.setdefault(container_id, [])
                        if restart_delta:
                            events.extend([event_now] * min(restart_delta, self.settings.restart_loop_threshold))
                        cutoff = event_now - timedelta(minutes=10)
                        events[:] = [stamp for stamp in events if stamp >= cutoff]
                        last_alert = self.restart_alerted_at.get(container_id)
                        restarting_transition = item.get("status") == "restarting" and old.get("status") != "restarting"
                        repeated_restarts = len(events) >= self.settings.restart_loop_threshold
                        alert_cooled_down = last_alert is None or event_now - last_alert >= timedelta(minutes=10)
                        if (restarting_transition or repeated_restarts) and alert_cooled_down:
                            self.restart_alerted_at[container_id] = event_now
                            await self.telegram.send(
                                chat_id,
                                f"⚠️ Possible restart loop: <code>{html.escape(item['name'])}</code> · "
                                f"{len(events)} restarts in 10m · total <code>{item.get('restart_count', 0)}</code>",
                                {"inline_keyboard": [[{"text": "Logs", "callback_data": f"logmenu:{container_id}"}, {"text": "Open", "callback_data": f"view:{container_id}"}]]},
                            )
                        if item.get("oom_killed") and not old.get("oom_killed"):
                            await self.telegram.send(chat_id, f"🧠 <code>{html.escape(item['name'])}</code> was killed because it ran out of memory.", {"inline_keyboard": [[{"text": "Open", "callback_data": f"view:{container_id}"}]]})

                missing = await asyncio.to_thread(self.db.disable_schedules_for_missing, {item["name"] for item in containers})
                if owner and missing:
                    await self.telegram.send(int(owner["chat_id"]), f"🕒 Disabled schedules for removed containers: <code>{html.escape(', '.join(missing))}</code>")
                self.known_states = current
                initialized = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("Container watcher error: %s", exc)
                if self.docker_reachable:
                    self.docker_reachable = False
                    owner = await asyncio.to_thread(self.db.get_owner)
                    if owner:
                        try:
                            await self.telegram.send(int(owner["chat_id"]), f"❌ Docker is unreachable: {html.escape(docker_error(exc))}")
                        except Exception:
                            pass
            await asyncio.sleep(self.settings.poll_interval_seconds)

    async def run_schedules(self) -> None:
        zone = ZoneInfo(self.settings.timezone)
        while not self.stop_event.is_set():
            try:
                now_local = datetime.now(zone)
                local_date = now_local.date().isoformat()
                schedules = await asyncio.to_thread(self.db.list_schedules, True)
                owner = await asyncio.to_thread(self.db.get_owner)
                for schedule in schedules:
                    if schedule.get("last_run_date") == local_date:
                        continue
                    due_time = datetime.strptime(schedule["time_hhmm"], "%H:%M").time()
                    due = datetime.combine(now_local.date(), due_time, tzinfo=zone)
                    if now_local < due or now_local > due + timedelta(minutes=60):
                        continue
                    failures = int(schedule.get("failure_count") or 0)
                    last_attempt_raw = schedule.get("last_attempt_at")
                    if last_attempt_raw:
                        last_attempt = datetime.fromisoformat(str(last_attempt_raw)).astimezone(zone)
                        if last_attempt.date() != now_local.date():
                            failures = 0
                        elif now_local - last_attempt < timedelta(minutes=self.settings.schedule_retry_minutes):
                            continue
                    if failures >= self.settings.schedule_max_attempts:
                        continue
                    await asyncio.to_thread(self.db.mark_schedule_attempt, schedule["id"], now_local)
                    try:
                        result = await asyncio.to_thread(self.docker.mutate, "restart", schedule["container_name"])
                        await asyncio.to_thread(self.db.mark_schedule_success, schedule["id"], local_date)
                        await asyncio.to_thread(self.db.audit, int(owner["user_id"]) if owner else 0, "scheduled.restart", result["name"], "success", schedule["id"])
                        if owner:
                            await self.telegram.send(int(owner["chat_id"]), f"🕒 Scheduled restart completed: <code>{html.escape(result['name'])}</code>", {"inline_keyboard": [[{"text": "Open", "callback_data": f"view:{result['id']}"}]]}, disable_notification=True)
                    except Exception as exc:
                        await asyncio.to_thread(self.db.mark_schedule_failure, schedule["id"], str(exc))
                        await asyncio.to_thread(self.db.audit, int(owner["user_id"]) if owner else 0, "scheduled.restart", schedule["container_name"], "failed", str(exc))
                        if owner:
                            attempt = failures + 1
                            await self.telegram.send(
                                int(owner["chat_id"]),
                                f"❌ Scheduled restart failed for <code>{html.escape(schedule['container_name'])}</code> (attempt {attempt}/{self.settings.schedule_max_attempts}): {html.escape(str(exc))}",
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("Scheduler error: %s", exc)
            await asyncio.sleep(20)
