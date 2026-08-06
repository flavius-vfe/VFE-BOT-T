from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import docker
from docker.errors import DockerException, NotFound


STATUS_ICONS = {
    "running": "🟢",
    "exited": "🔴",
    "restarting": "🟡",
    "paused": "🟠",
    "created": "⚪",
    "dead": "⚫",
}


class DockerService:
    def __init__(self, protected: set[str], host_storage_path: str, client: Any | None = None):
        self.client = client or docker.from_env()
        self.protected = protected
        self.host_storage_path = host_storage_path

    def ping(self) -> bool:
        return bool(self.client.ping())

    def list_containers(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for container in self.client.containers.list(all=True):
            container.reload()
            result.append(
                {
                    "id": container.short_id,
                    "name": container.name,
                    "status": container.status,
                    "image": container.attrs.get("Config", {}).get("Image", "unknown"),
                    "protected": container.name in self.protected,
                }
            )
        return sorted(result, key=lambda item: item["name"].lower())

    def resolve(self, value: str):
        value = value.strip()
        if not value:
            raise ValueError("Container name is required")
        try:
            return self.client.containers.get(value)
        except NotFound:
            pass

        containers = self.client.containers.list(all=True)
        exact_ci = [c for c in containers if c.name.lower() == value.lower()]
        if len(exact_ci) == 1:
            return exact_ci[0]
        prefix = [c for c in containers if c.name.lower().startswith(value.lower())]
        if len(prefix) == 1:
            return prefix[0]
        if len(prefix) > 1:
            names = ", ".join(c.name for c in prefix[:8])
            raise ValueError(f"Ambiguous container name: {names}")
        raise ValueError(f"Container not found: {value}")

    def is_protected(self, container: Any) -> bool:
        return container.name in self.protected

    def info(self, value: str) -> dict[str, Any]:
        container = self.resolve(value)
        container.reload()
        attrs = container.attrs
        state = attrs.get("State", {})
        config = attrs.get("Config", {})
        host = attrs.get("HostConfig", {})
        networks = attrs.get("NetworkSettings", {}).get("Networks", {})
        return {
            "id": container.short_id,
            "name": container.name,
            "status": container.status,
            "image": config.get("Image", "unknown"),
            "created": attrs.get("Created", ""),
            "started": state.get("StartedAt", ""),
            "restart_policy": host.get("RestartPolicy", {}).get("Name", "none"),
            "networks": sorted(networks.keys()),
            "protected": self.is_protected(container),
        }

    def logs(self, value: str, lines: int) -> str:
        container = self.resolve(value)
        output = container.logs(tail=lines, timestamps=True)
        return output.decode("utf-8", errors="replace")

    def stats(self, value: str) -> dict[str, Any]:
        container = self.resolve(value)
        data = container.stats(stream=False)
        cpu_stats = data.get("cpu_stats", {})
        precpu_stats = data.get("precpu_stats", {})
        cpu_delta = (
            cpu_stats.get("cpu_usage", {}).get("total_usage", 0)
            - precpu_stats.get("cpu_usage", {}).get("total_usage", 0)
        )
        system_delta = cpu_stats.get("system_cpu_usage", 0) - precpu_stats.get("system_cpu_usage", 0)
        online_cpus = cpu_stats.get("online_cpus") or len(
            cpu_stats.get("cpu_usage", {}).get("percpu_usage", [])
        ) or 1
        cpu_percent = (cpu_delta / system_delta * online_cpus * 100) if system_delta > 0 else 0.0

        memory = data.get("memory_stats", {})
        usage = int(memory.get("usage", 0))
        cache = int(memory.get("stats", {}).get("cache", 0))
        effective_usage = max(0, usage - cache)
        limit = int(memory.get("limit", 0))
        memory_percent = effective_usage / limit * 100 if limit else 0.0

        networks = data.get("networks", {})
        rx = sum(int(item.get("rx_bytes", 0)) for item in networks.values())
        tx = sum(int(item.get("tx_bytes", 0)) for item in networks.values())
        return {
            "name": container.name,
            "cpu_percent": round(cpu_percent, 2),
            "memory_usage": effective_usage,
            "memory_limit": limit,
            "memory_percent": round(memory_percent, 2),
            "network_rx": rx,
            "network_tx": tx,
        }

    def mutate(self, action: str, value: str) -> dict[str, Any]:
        container = self.resolve(value)
        if self.is_protected(container):
            raise PermissionError(f"Protected container: {container.name}")
        if action == "start":
            container.start()
        elif action == "stop":
            container.stop(timeout=20)
        elif action == "restart":
            container.restart(timeout=20)
        else:
            raise ValueError(f"Unsupported action: {action}")
        container.reload()
        return {"name": container.name, "status": container.status, "action": action}

    def server_info(self) -> dict[str, Any]:
        info = self.client.info()
        version = self.client.version()
        storage: dict[str, Any] | None = None
        path = Path(self.host_storage_path)
        if path.exists():
            usage = shutil.disk_usage(path)
            storage = {"path": str(path), "total": usage.total, "used": usage.used, "free": usage.free}
        return {
            "name": info.get("Name", "unknown"),
            "docker_version": version.get("Version", "unknown"),
            "containers": info.get("Containers", 0),
            "containers_running": info.get("ContainersRunning", 0),
            "containers_stopped": info.get("ContainersStopped", 0),
            "cpus": info.get("NCPU", 0),
            "memory_total": info.get("MemTotal", 0),
            "storage": storage,
        }


def human_bytes(value: int | float) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PiB"


def docker_error(exc: Exception) -> str:
    if isinstance(exc, DockerException):
        return f"Docker error: {exc}"
    return str(exc)
