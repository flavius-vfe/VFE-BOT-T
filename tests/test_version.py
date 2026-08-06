from pathlib import Path

from vfe_bot import __version__


def test_version_is_read_from_version_file() -> None:
    expected = (Path(__file__).parents[1] / "VERSION").read_text(encoding="utf-8").strip()
    assert __version__ == expected
