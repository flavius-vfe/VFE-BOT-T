from __future__ import annotations

import sys
import types
import xml.etree.ElementTree as ET

import yaml

try:
    import docker  # type: ignore # noqa: F401
except ModuleNotFoundError:
    docker_module = types.ModuleType("docker")
    errors_module = types.ModuleType("docker.errors")

    class DockerException(Exception):
        pass

    class NotFound(DockerException):
        pass

    docker_module.from_env = lambda: None  # type: ignore[attr-defined]
    errors_module.DockerException = DockerException  # type: ignore[attr-defined]
    errors_module.NotFound = NotFound  # type: ignore[attr-defined]
    sys.modules["docker"] = docker_module
    sys.modules["docker.errors"] = errors_module

from vfe_bot.docker_service import DockerService


class FakeContainer:
    def __init__(self, name: str, status: str, short_id: str):
        self.name = name
        self.status = status
        self.short_id = short_id
        self.attrs = {
            "Config": {
                "Image": f"example/{name}:latest",
                "Env": ["NORMAL=value", "API_TOKEN=very-secret", "PLEX_CLAIM=claim-secret"],
                "Labels": {"app": name},
                "Cmd": ["--serve"],
                "Entrypoint": ["/entrypoint"],
                "WorkingDir": "/app",
                "User": "1000:1000",
                "Hostname": name,
            },
            "State": {"StartedAt": "now", "Health": {"Status": "healthy"}},
            "HostConfig": {
                "RestartPolicy": {"Name": "unless-stopped"},
                "NetworkMode": "bridge",
                "PortBindings": {"32400/tcp": [{"HostIp": "", "HostPort": "32400"}]},
                "Devices": [],
                "Privileged": False,
                "CapAdd": [],
                "Dns": [],
                "ExtraHosts": [],
            },
            "NetworkSettings": {"Networks": {"bridge": {}}},
            "Mounts": [
                {
                    "Source": "/mnt/user/appdata/plex",
                    "Destination": "/config",
                    "RW": True,
                    "Type": "bind",
                }
            ],
            "Created": "today",
        }

    def reload(self) -> None:
        return None

    def start(self) -> None:
        self.status = "running"

    def stop(self, timeout: int) -> None:
        assert timeout == 20
        self.status = "exited"

    def restart(self, timeout: int) -> None:
        assert timeout == 20
        self.status = "running"


class FakeContainers:
    def __init__(self, items: list[FakeContainer]):
        self.items = items

    def list(self, all: bool = False):  # noqa: A002
        assert all
        return self.items

    def get(self, value: str):
        for item in self.items:
            if value in {item.name, item.short_id}:
                return item
        from docker.errors import NotFound

        raise NotFound(value)


class FakeClient:
    def __init__(self):
        self.containers = FakeContainers(
            [FakeContainer("plex", "running", "abc123"), FakeContainer("vfe-bot-t", "running", "def456")]
        )


def test_auto_discovery_and_protection(tmp_path) -> None:
    service = DockerService({"vfe-bot-t"}, str(tmp_path), client=FakeClient())
    items = service.list_containers()
    assert [item["name"] for item in items] == ["plex", "vfe-bot-t"]
    assert items[1]["protected"] is True
    assert service.mutate("restart", "plex")["status"] == "running"

    try:
        service.mutate("stop", "vfe-bot-t")
    except PermissionError:
        pass
    else:
        raise AssertionError("protected container mutation should fail")


def test_yaml_and_unraid_xml_export_redact_secrets(tmp_path) -> None:
    service = DockerService({"vfe-bot-t"}, str(tmp_path), client=FakeClient())

    yaml_name, yaml_bytes, yaml_type = service.export_profile("plex", "yaml")
    assert yaml_name == "plex.compose.yaml"
    assert yaml_type == "application/yaml"
    yaml_text = yaml_bytes.decode()
    assert yaml_text.startswith("# Exported by VFE Docker Bot")
    compose = yaml.safe_load(yaml_text)
    plex = compose["services"]["plex"]
    assert plex["image"] == "example/plex:latest"
    assert plex["environment"]["NORMAL"] == "value"
    assert plex["environment"]["API_TOKEN"] == "<redacted>"
    assert plex["environment"]["PLEX_CLAIM"] == "<redacted>"
    assert "32400:32400/tcp" in plex["ports"]
    assert "/mnt/user/appdata/plex:/config:rw" in plex["volumes"]

    xml_name, xml_bytes, xml_type = service.export_profile("plex", "xml")
    assert xml_name == "my-plex.xml"
    assert xml_type == "application/xml"
    root = ET.fromstring(xml_bytes)
    assert root.tag == "Container"
    assert root.findtext("Name") == "plex"
    assert root.findtext("Repository") == "example/plex:latest"
    variables = {item.attrib.get("Name"): item.text for item in root.findall("Config") if item.attrib.get("Type") == "Variable"}
    assert variables["NORMAL"] == "value"
    assert variables["API_TOKEN"] == "<redacted>"
