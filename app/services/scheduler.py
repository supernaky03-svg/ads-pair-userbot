from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from app.config import Settings
from app.db.session import Database
from app.services.pair_runner import PairRunner

logger = logging.getLogger(__name__)


class DailyScheduler:
    def __init__(self, *, db: Database, runner: PairRunner, settings: Settings, notify_control) -> None:
        self.db = db
        self.runner = runner
        self.settings = settings
        self.notify_control = notify_control
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="daily-scheduler")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                results = await self.runner.run_due()
                if results:
                    now = datetime.now(self.settings.tzinfo)
                    ok = sum(1 for item in results if item.ok)
                    failed = len(results) - ok
                    lines = [
                        f"⏰ Scheduled scan {now:%Y-%m-%d %H:%M} {self.settings.timezone}",
                        f"Pairs handled: {len(results)} | OK: {ok} | Failed: {failed}",
                    ]
                    for item in results:
                        mark = "✅" if item.ok else "❌"
                        lines.append(f"{mark} Pair {item.pair_id}: {item.message}")
                    await self.notify_control("\n".join(lines))
            except Exception as exc:
                logger.exception("Daily scheduler failed")
                try:
                    await self.notify_control(f"❌ Scheduler error: {type(exc).__name__}: {exc}")
                except Exception:
                    logger.exception("Failed to notify control chat about scheduler error")
            await asyncio.sleep(30)
