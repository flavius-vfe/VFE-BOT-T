from __future__ import annotations

import asyncio
import logging
import signal

from .bot import VFEBot
from .database import Database
from .docker_service import DockerService
from .settings import Settings
from .telegram_api import TelegramAPI


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def run() -> None:
    configure_logging()
    settings = Settings.load()
    db = Database(settings.database_path)
    db.initialize()
    docker = DockerService(settings.protected_containers, settings.host_storage_path)
    if not await asyncio.to_thread(docker.ping):
        raise RuntimeError("Docker daemon is not reachable")
    telegram = TelegramAPI(settings.telegram_token)
    bot = VFEBot(settings, db, docker, telegram)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, bot.stop_event.set)
        except NotImplementedError:
            pass

    try:
        await bot.start()
    finally:
        await telegram.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
