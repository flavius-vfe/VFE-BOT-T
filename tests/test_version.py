from vfe_bot import __version__


def test_version_is_read_from_version_file() -> None:
    assert __version__ == "0.4.0"
