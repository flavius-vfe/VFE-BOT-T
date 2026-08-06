from vfe_bot.parser import parse_command


def test_commands() -> None:
    assert parse_command("/restart plex") == ("restart", ["plex"])
    assert parse_command("logs sonarr 100") == ("logs", ["sonarr", "100"])
    assert parse_command("status") == ("server", [])
    assert parse_command("list") == ("containers", [])
    assert parse_command("start plex") == ("startc", ["plex"])
    assert parse_command("/start") == ("start", [])
