import os
import shutil
import subprocess
from pathlib import Path


def make_fake_docker(fake_bin: Path) -> None:
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1 $2\" == \"compose version\" ]]; then exit 0; fi\n"
        "if [[ \"$1 $2 $3\" == \"compose images -q\" ]]; then echo fake-image-id; exit 0; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)


def make_installation(tmp_path: Path, source_script: Path) -> Path:
    install = tmp_path / "vfe-bot-t"
    (install / "vfe_bot").mkdir(parents=True)
    (install / "data").mkdir()
    (install / "data" / "vfe-bot.db").write_text("database", encoding="utf-8")
    (install / ".env").write_text("TELEGRAM_TOKEN=test\n", encoding="utf-8")
    (install / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    shutil.copy2(source_script, install / "uninstall.sh")
    return install


def run_uninstall(install: Path, fake_bin: Path, *args: str) -> None:
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    subprocess.run(
        ["bash", str(install / "uninstall.sh"), "--install-dir", str(install), "--yes", *args],
        check=True,
        env=env,
    )


def test_uninstall_keeps_configuration_by_default(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "uninstall.sh"
    install = make_installation(tmp_path, source)
    fake_bin = tmp_path / "bin"
    make_fake_docker(fake_bin)

    run_uninstall(install, fake_bin)

    assert install.is_dir()
    assert (install / ".env").is_file()
    assert (install / "data" / "vfe-bot.db").is_file()


def test_uninstall_purge_removes_only_verified_project(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "uninstall.sh"
    install = make_installation(tmp_path, source)
    fake_bin = tmp_path / "bin"
    make_fake_docker(fake_bin)

    run_uninstall(install, fake_bin, "--purge")

    assert not install.exists()
