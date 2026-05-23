from __future__ import annotations

import asyncio
import logging
import signal

from app.config import load_settings
from app.db.session import Database
from app.logging_config import setup_logging
from app.services.pair_runner import PairRunner
from app.services.pair_service import PairService
from app.services.scheduler import DailyScheduler
from app.services.telegram_service import TelegramService
from app.telegram.client import create_client
from app.telegram.commands import CommandContext, CommandRouter
from app.web.health import HealthServer

logger = logging.getLogger(__name__)


async def main() -> None:
    setup_logging()
    settings = load_settings()

    db = Database(settings.database_url)
    await db.init()

    client = create_client(settings)
    await client.start()
    me = await client.get_me()
    logger.info("Logged in as %s (%s)", getattr(me, "username", None) or getattr(me, "first_name", None), me.id)

    telegram = TelegramService(client)
    pair_runner = PairRunner(db=db, telegram=telegram, settings=settings)
    pair_service = PairService(db=db, telegram=telegram, settings=settings)

    ctx = CommandContext(
        db=db,
        settings=settings,
        pair_service=pair_service,
        runner=pair_runner,
        self_id=int(me.id),
    )
    router = CommandRouter(client, ctx)
    router.register()

    health = HealthServer(host=settings.host, port=settings.port, path=settings.health_path)
    await health.start()
    logger.info("Health server listening on %s:%s%s", settings.host, settings.port, settings.health_path)

    scheduler = DailyScheduler(db=db, runner=pair_runner, settings=settings, notify_control=router.send_control)
    scheduler.start()

    await router.send_control(
        "✅ Session Pair Userbot started\n"
        f"Account: @{getattr(me, 'username', None) or me.id}\n"
        f"Control mode: {settings.control_mode}\n"
        f"Daily scan: {settings.daily_run_time} {settings.timezone}\n"
        f"Retry: every {settings.retry_interval_minutes} min until {settings.cutoff_time}\n"
        f"Job delay: {settings.job_delay_seconds}s"
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    await stop_event.wait()
    logger.info("Stopping service...")
    await scheduler.stop()
    await health.stop()
    await client.disconnect()
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
