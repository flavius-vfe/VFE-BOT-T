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
