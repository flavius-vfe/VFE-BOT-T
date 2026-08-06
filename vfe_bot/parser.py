from __future__ import annotations


def parse_command(text: str) -> tuple[str, list[str]]:
    parts = text.strip().split()
    if not parts:
        return "", []
    command = parts[0].lstrip("/").split("@", 1)[0].lower()
    args = parts[1:]
    if command == "start" and args:
        command = "startc"
    aliases = {
        "docker": "containers",
        "list": "containers",
        "status": "server",
        "run": "startc",
    }
    return aliases.get(command, command), args
