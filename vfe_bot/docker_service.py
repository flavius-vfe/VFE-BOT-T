from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import zipfile
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
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
    "removing": "🟣",
}

STATUS_LABELS = {
    "running": "RUNNING",
    "exited": "STOPPED",
    "restarting": "RESTARTING",
    "paused": "PAUSED",
    "created": "CREATED",
    "dead": "DEAD",
    "removing": "REMOVING",
}

_SENSITIVE_KEY_RE = re.compile(
    r"(?:PASSWORD|PASSWD|TOKEN|SECRET|API[_-]?KEY|PRIVATE[_-]?KEY|ACCESS[_-]?KEY|AUTH|CREDENTIAL|CLAIM|COOKIE|SESSION)",
    re.IGNORECASE,
)
_URL_CREDENTIAL_RE = re.compile(r"(?P<scheme>https?://)(?P<userinfo>[^/@\s]+)@", re.IGNORECASE)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value or value.startswith("0001-"):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (ValueError, TypeError):
        return None


def human_duration(seconds: float | int) -> str:
    total = max(0, int(seconds))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


class DockerService:
    def __init__(self, protected: set[str], host_storage_path: str, client: Any | None = None):
        self.client = client or docker.from_env()
        self.protected = protected
        self.host_storage_path = host_storage_path

    def ping(self) -> bool:
        return bool(self.client.ping())

    @staticmethod
    def project_token(project: str) -> str:
        return hashlib.sha256(project.encode("utf-8")).hexdigest()[:12]

    def _summary(self, container: Any) -> dict[str, Any]:
        container.reload()
        attrs = container.attrs or {}
        state = attrs.get("State", {}) or {}
        config = attrs.get("Config", {}) or {}
        labels = config.get("Labels", {}) or {}
        status = str(state.get("Status") or getattr(container, "status", "unknown") or "unknown").lower()
        health = (state.get("Health") or {}).get("Status")
        started_at = _parse_datetime(state.get("StartedAt"))
        uptime_seconds = 0
        if status == "running" and started_at:
            uptime_seconds = max(0, int((datetime.now(UTC) - started_at).total_seconds()))
        compose_project = labels.get("com.docker.compose.project")
        project = str(compose_project or f"Standalone: {container.name}")
        service = str(labels.get("com.docker.compose.service") or container.name)
        return {
            "id": container.short_id,
            "name": container.name,
            "status": status,
            "health": str(health).lower() if health else None,
            "image": config.get("Image", "unknown"),
            "protected": container.name in self.protected,
            "project": project,
            "project_token": self.project_token(project),
            "compose_service": service,
            "restart_count": int(attrs.get("RestartCount", 0) or 0),
            "exit_code": int(state.get("ExitCode", 0) or 0),
            "oom_killed": bool(state.get("OOMKilled", False)),
            "error": str(state.get("Error") or ""),
            "started": state.get("StartedAt", ""),
            "uptime_seconds": uptime_seconds,
            "uptime": human_duration(uptime_seconds) if uptime_seconds else "—",
        }

    def list_containers(self) -> list[dict[str, Any]]:
        result = [self._summary(container) for container in self.client.containers.list(all=True)]
        return sorted(result, key=lambda item: (item["project"].lower(), item["name"].lower()))

    def list_projects(self) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for item in self.list_containers():
            groups.setdefault(str(item["project"]), []).append(item)
        result: list[dict[str, Any]] = []
        for project, containers in groups.items():
            result.append(
                {
                    "name": project,
                    "token": self.project_token(project),
                    "containers": containers,
                    "running": sum(1 for item in containers if item["status"] == "running"),
                    "stopped": sum(1 for item in containers if item["status"] == "exited"),
                    "unhealthy": sum(1 for item in containers if item.get("health") == "unhealthy"),
                    "protected": all(bool(item["protected"]) for item in containers),
                }
            )
        return sorted(result, key=lambda item: item["name"].lower())

    def resolve_project_token(self, token: str) -> str:
        matches = [item["name"] for item in self.list_projects() if item["token"] == token]
        if len(matches) != 1:
            raise ValueError("Compose project not found")
        return str(matches[0])

    def project_info(self, project_or_token: str) -> dict[str, Any]:
        project = project_or_token
        if re.fullmatch(r"[0-9a-f]{12}", project_or_token):
            project = self.resolve_project_token(project_or_token)
        for item in self.list_projects():
            if item["name"] == project:
                return item
        raise ValueError(f"Compose project not found: {project}")

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
        summary = self._summary(container)
        attrs = container.attrs or {}
        config = attrs.get("Config", {}) or {}
        host = attrs.get("HostConfig", {}) or {}
        network_settings = attrs.get("NetworkSettings", {}) or {}
        networks = network_settings.get("Networks", {}) or {}
        mounts = attrs.get("Mounts", []) or []
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
            **summary,
            "created": attrs.get("Created", ""),
            "restart_policy": host.get("RestartPolicy", {}).get("Name", "none"),
            "network_mode": host.get("NetworkMode", "default"),
            "networks": sorted(networks.keys()),
            "ports": ports,
            "mount_count": len(mounts),
        }

    def logs(self, value: str, lines: int) -> str:
        container = self.resolve(value)
        output = container.logs(tail=lines, timestamps=True)
        if isinstance(output, bytes):
            return output.decode("utf-8", errors="replace")
        return str(output)

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
        stats_map = memory.get("stats", {}) or {}
        cache = int(stats_map.get("inactive_file", stats_map.get("cache", 0)) or 0)
        effective_usage = max(0, usage - cache)
        limit = int(memory.get("limit", 0))
        memory_percent = effective_usage / limit * 100 if limit else 0.0

        networks = data.get("networks", {}) or {}
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

    def mutate_project(self, action: str, project_or_token: str) -> dict[str, Any]:
        project = self.project_info(project_or_token)
        candidates = [item for item in project["containers"] if not item["protected"]]
        if not candidates:
            raise PermissionError("All containers in this project are protected")
        results: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        ordered = sorted(candidates, key=lambda item: item["name"].lower(), reverse=action == "stop")
        for item in ordered:
            if action == "start" and item["status"] == "running":
                continue
            if action in {"stop", "restart"} and item["status"] != "running":
                continue
            try:
                results.append(self.mutate(action, item["id"]))
            except Exception as exc:
                errors.append({"name": item["name"], "error": docker_error(exc)})
        if errors and not results:
            detail = "; ".join(f"{item['name']}: {item['error']}" for item in errors)
            raise RuntimeError(f"Stack action failed: {detail}")
        return {"project": project["name"], "action": action, "results": results, "errors": errors}

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
    def _redact_text(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        return _URL_CREDENTIAL_RE.sub(lambda match: f"{match.group('scheme')}<redacted>@", value)

    @classmethod
    def _redacted_mapping(cls, values: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
        result: dict[str, Any] = {}
        redacted: list[str] = []
        for key, value in (values or {}).items():
            if _SENSITIVE_KEY_RE.search(str(key)):
                result[str(key)] = "<redacted>"
                redacted.append(str(key))
            else:
                result[str(key)] = cls._redact_text(value)
        return result, redacted

    @classmethod
    def _redacted_environment(cls, env_items: list[str] | None) -> tuple[dict[str, str], list[str]]:
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
                environment[key] = str(cls._redact_text(value))
        return environment, redacted

    def _profile(self, value: str) -> dict[str, Any]:
        container = self.resolve(value)
        container.reload()
        attrs = container.attrs or {}
        config = attrs.get("Config", {}) or {}
        host = attrs.get("HostConfig", {}) or {}
        environment, redacted_env = self._redacted_environment(config.get("Env"))
        labels, redacted_labels = self._redacted_mapping(config.get("Labels") or {})
        # Compose generates these labels itself; exporting stale runtime labels can break restores.
        labels = {key: value for key, value in labels.items() if not key.startswith("com.docker.compose.")}

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
            "project": (config.get("Labels") or {}).get("com.docker.compose.project") or f"Standalone: {container.name}",
            "image": config.get("Image", "unknown"),
            "container_name": container.name,
            "restart": (host.get("RestartPolicy") or {}).get("Name") or "no",
            "network_mode": network_mode,
            "environment": environment,
            "ports": ports,
            "volumes": volumes,
            "devices": devices,
            "labels": labels,
            "command": [self._redact_text(item) for item in (config.get("Cmd") or [])] or None,
            "entrypoint": [self._redact_text(item) for item in (config.get("Entrypoint") or [])] or None,
            "working_dir": config.get("WorkingDir") or None,
            "user": config.get("User") or None,
            "hostname": config.get("Hostname") or None,
            "privileged": bool(host.get("Privileged", False)),
            "cap_add": host.get("CapAdd") or [],
            "dns": host.get("Dns") or [],
            "extra_hosts": host.get("ExtraHosts") or [],
            "redacted_keys": sorted(set(redacted_env + redacted_labels)),
        }
        return profile

    @staticmethod
    def _safe_name(name: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-") or "container"

    @staticmethod
    def _yaml_service(profile: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in profile.items()
            if key not in {"name", "project", "redacted_keys"} and value not in (None, [], {}, "")
        }

    def _xml_profile(self, profile: dict[str, Any]) -> bytes:
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
            "Overview": "Exported by VFE Docker Bot. Sensitive values are redacted.",
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
            element = ET.SubElement(root, "Config", {
                "Name": key, "Target": key, "Default": "", "Mode": "", "Description": "",
                "Type": "Variable", "Display": "always", "Required": "false",
                "Mask": "true" if value == "<redacted>" else "false",
            })
            element.text = value
        for port in profile["ports"]:
            host_part, _, target = port.rpartition(":")
            target_port, _, protocol = target.partition("/")
            host_port = host_part.rsplit(":", 1)[-1]
            element = ET.SubElement(root, "Config", {
                "Name": f"Port {target}", "Target": target_port, "Default": "", "Mode": protocol or "tcp",
                "Description": "", "Type": "Port", "Display": "always", "Required": "false", "Mask": "false",
            })
            element.text = host_port
        for index, volume in enumerate(profile["volumes"], start=1):
            source, destination, mode = volume.rsplit(":", 2)
            element = ET.SubElement(root, "Config", {
                "Name": f"Path {index}", "Target": destination, "Default": "", "Mode": mode,
                "Description": "", "Type": "Path", "Display": "always", "Required": "false", "Mask": "false",
            })
            element.text = source
        ET.indent(root, space="  ")
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    def export_profile(self, value: str, file_format: str) -> tuple[str, bytes, str]:
        profile = self._profile(value)
        safe_name = self._safe_name(profile["name"])
        file_format = file_format.lower()
        if file_format == "yaml":
            document = {"services": {safe_name: self._yaml_service(profile)}}
            content = (
                "# Exported by VFE Docker Bot. Sensitive values are redacted.\n"
                + yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
            ).encode("utf-8")
            return f"{safe_name}.compose.yaml", content, "application/yaml"
        if file_format == "xml":
            return f"my-{safe_name}.xml", self._xml_profile(profile), "application/xml"
        raise ValueError("Export format must be yaml or xml")

    def _export_bundle(self, profiles: list[dict[str, Any]], bundle_name: str) -> tuple[str, bytes, str]:
        compose = {
            "services": {
                self._safe_name(profile["name"]): self._yaml_service(profile)
                for profile in profiles
            }
        }
        manifest = {
            "exported_at": datetime.now(UTC).isoformat(),
            "container_count": len(profiles),
            "containers": [
                {
                    "name": profile["name"],
                    "image": profile["image"],
                    "project": profile["project"],
                    "redacted_keys": profile["redacted_keys"],
                }
                for profile in profiles
            ],
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                f"{bundle_name}.compose.yaml",
                "# Exported by VFE Docker Bot. Sensitive values are redacted.\n"
                + yaml.safe_dump(compose, sort_keys=False, allow_unicode=True),
            )
            archive.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
            for profile in profiles:
                archive.writestr(f"unraid/my-{self._safe_name(profile['name'])}.xml", self._xml_profile(profile))
        return f"{bundle_name}.zip", buffer.getvalue(), "application/zip"

    def export_project(self, project_or_token: str) -> tuple[str, bytes, str]:
        project = self.project_info(project_or_token)
        profiles = [self._profile(item["id"]) for item in project["containers"]]
        return self._export_bundle(profiles, f"stack-{self._safe_name(project['name'])}")

    def export_all(self) -> tuple[str, bytes, str]:
        profiles = [self._profile(item["id"]) for item in self.list_containers()]
        return self._export_bundle(profiles, "all-container-profiles")

    def diagnostics(self) -> dict[str, Any]:
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "server": self.server_info(),
            "containers": self.list_containers(),
            "projects": [
                {
                    "name": item["name"],
                    "running": item["running"],
                    "stopped": item["stopped"],
                    "unhealthy": item["unhealthy"],
                    "container_names": [container["name"] for container in item["containers"]],
                }
                for item in self.list_projects()
            ],
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
