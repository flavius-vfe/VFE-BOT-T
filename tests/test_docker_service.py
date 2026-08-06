from __future__ import annotations

import sys
import types

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
            "Config": {"Image": f"example/{name}:latest"},
            "State": {"StartedAt": "now"},
            "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
            "NetworkSettings": {"Networks": {"bridge": {}}},
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
