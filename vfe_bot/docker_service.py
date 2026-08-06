from __future__ import annotations

import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import docker
import yaml
from docker.errors import DockerException, NotFound


STATUS_ICONS = {
    "running": "🟢",
    "exited": "🔴",
    "restarting": "🟡",
    "paused": "🟠",
    "created": "⚪",
    "dead": "⚫",
}

_SENSITIVE_KEY_RE = re.compile(
    r"(?:PASSWORD|PASSWD|TOKEN|SECRET|API[_-]?KEY|PRIVATE[_-]?KEY|ACCESS[_-]?KEY|AUTH|CREDENTIAL|CLAIM)",
    re.IGNORECASE,
)


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
        network_settings = attrs.get("NetworkSettings", {})
        networks = network_settings.get("Networks", {})
        mounts = attrs.get("Mounts", []) or []
        health = state.get("Health", {}).get("Status")
        port_bindings = host.get("PortBindings", {}) or {}
        ports: list[str] = []
        for target, bindings in sorted(port_bindings.items()):
            if not bindings:
                ports.append(str(target))
                continue
            for binding in bindings:
                host_ip = binding.get("HostIp", "")
                host_port = binding.get("HostPort", "")
                prefix = f"{host_ip}:" if host_ip and host_ip not in {"0.0.0.0", "::"} else ""
                ports.append(f"{prefix}{host_port}→{target}")
        return {
            "id": container.short_id,
            "name": container.name,
            "status": container.status,
            "image": config.get("Image", "unknown"),
            "created": attrs.get("Created", ""),
            "started": state.get("StartedAt", ""),
            "health": health,
            "restart_policy": host.get("RestartPolicy", {}).get("Name", "none"),
            "network_mode": host.get("NetworkMode", "default"),
            "networks": sorted(networks.keys()),
            "ports": ports,
            "mount_count": len(mounts),
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
            "id": container.short_id,
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
        return {"name": container.name, "status": container.status, "action": action, "id": container.short_id}

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

    @staticmethod
    def _redacted_environment(env_items: list[str] | None) -> tuple[dict[str, str], list[str]]:
        environment: dict[str, str] = {}
        redacted: list[str] = []
        for item in env_items or []:
            key, separator, value = item.partition("=")
            if not separator:
                environment[key] = ""
            elif _SENSITIVE_KEY_RE.search(key):
                environment[key] = "<redacted>"
                redacted.append(key)
            else:
                environment[key] = value
        return environment, redacted

    def _profile(self, value: str) -> dict[str, Any]:
        container = self.resolve(value)
        container.reload()
        attrs = container.attrs
        config = attrs.get("Config", {}) or {}
        host = attrs.get("HostConfig", {}) or {}
        environment, redacted = self._redacted_environment(config.get("Env"))

        ports: list[str] = []
        for target, bindings in sorted((host.get("PortBindings") or {}).items()):
            if not bindings:
                ports.append(str(target))
                continue
            for binding in bindings:
                host_ip = binding.get("HostIp", "")
                host_port = binding.get("HostPort", "")
                prefix = f"{host_ip}:" if host_ip and host_ip not in {"0.0.0.0", "::"} else ""
                ports.append(f"{prefix}{host_port}:{target}")

        volumes: list[str] = []
        for mount in attrs.get("Mounts", []) or []:
            source = mount.get("Source", "")
            destination = mount.get("Destination", "")
            if not source or not destination:
                continue
            mode = "rw" if mount.get("RW", True) else "ro"
            volumes.append(f"{source}:{destination}:{mode}")

        devices: list[str] = []
        for device in host.get("Devices") or []:
            host_path = device.get("PathOnHost", "")
            container_path = device.get("PathInContainer", "")
            permissions = device.get("CgroupPermissions", "rwm")
            if host_path and container_path:
                devices.append(f"{host_path}:{container_path}:{permissions}")

        network_mode = host.get("NetworkMode") or "bridge"
        profile: dict[str, Any] = {
            "name": container.name,
            "image": config.get("Image", "unknown"),
            "container_name": container.name,
            "restart": (host.get("RestartPolicy") or {}).get("Name") or "no",
            "network_mode": network_mode,
            "environment": environment,
            "ports": ports,
            "volumes": volumes,
            "devices": devices,
            "labels": config.get("Labels") or {},
            "command": config.get("Cmd"),
            "entrypoint": config.get("Entrypoint"),
            "working_dir": config.get("WorkingDir") or None,
            "user": config.get("User") or None,
            "hostname": config.get("Hostname") or None,
            "privileged": bool(host.get("Privileged", False)),
            "cap_add": host.get("CapAdd") or [],
            "dns": host.get("Dns") or [],
            "extra_hosts": host.get("ExtraHosts") or [],
            "redacted_environment_keys": redacted,
        }
        return profile

    def export_profile(self, value: str, file_format: str) -> tuple[str, bytes, str]:
        profile = self._profile(value)
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", profile["name"]).strip("-") or "container"
        file_format = file_format.lower()
        if file_format == "yaml":
            service = {
                key: value
                for key, value in profile.items()
                if key not in {"name", "redacted_environment_keys"} and value not in (None, [], {}, "")
            }
            document = {"services": {safe_name: service}}
            yaml_text = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
            content = (
                "# Exported by VFE Docker Bot. Sensitive environment values are redacted.\n"
                + yaml_text
            ).encode("utf-8")
            return f"{safe_name}.compose.yaml", content, "application/yaml"
        if file_format == "xml":
            root = ET.Element("Container", {"version": "2"})
            fields = {
                "Name": profile["name"],
                "Repository": profile["image"],
                "Registry": "",
                "Network": profile["network_mode"],
                "MyIP": "",
                "Shell": "sh",
                "Privileged": "true" if profile["privileged"] else "false",
                "Support": "",
                "Project": "",
                "Overview": "Exported by VFE Docker Bot. Sensitive environment values are redacted.",
                "Category": "Tools:",
                "WebUI": "",
                "TemplateURL": "",
                "Icon": "",
                "ExtraParams": "",
                "PostArgs": "",
                "CPUset": "",
                "DonateText": "",
                "DonateLink": "",
                "Requires": "",
            }
            for key, value in fields.items():
                ET.SubElement(root, key).text = str(value)

            for key, value in profile["environment"].items():
                element = ET.SubElement(
                    root,
                    "Config",
                    {
                        "Name": key,
                        "Target": key,
                        "Default": "",
                        "Mode": "",
                        "Description": "",
                        "Type": "Variable",
                        "Display": "always",
                        "Required": "false",
                        "Mask": "true" if value == "<redacted>" else "false",
                    },
                )
                element.text = value

            for port in profile["ports"]:
                host_part, _, target = port.rpartition(":")
                target_port, _, protocol = target.partition("/")
                host_port = host_part.rsplit(":", 1)[-1]
                element = ET.SubElement(
                    root,
                    "Config",
                    {
                        "Name": f"Port {target}",
                        "Target": target_port,
                        "Default": "",
                        "Mode": protocol or "tcp",
                        "Description": "",
                        "Type": "Port",
                        "Display": "always",
                        "Required": "false",
                        "Mask": "false",
                    },
                )
                element.text = host_port

            for index, volume in enumerate(profile["volumes"], start=1):
                source, destination, mode = volume.rsplit(":", 2)
                element = ET.SubElement(
                    root,
                    "Config",
                    {
                        "Name": f"Path {index}",
                        "Target": destination,
                        "Default": "",
                        "Mode": mode,
                        "Description": "",
                        "Type": "Path",
                        "Display": "always",
                        "Required": "false",
                        "Mask": "false",
                    },
                )
                element.text = source

            ET.indent(root, space="  ")
            content = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            return f"my-{safe_name}.xml", content, "application/xml"
        raise ValueError("Export format must be yaml or xml")


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
