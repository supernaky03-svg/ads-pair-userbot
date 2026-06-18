from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, time, timedelta

from app.config import Settings
from app.db.session import Database
from app.services.pair_runner import PairRunner

logger = logging.getLogger(__name__)


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(hour=int(hour), minute=int(minute))


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

    def _next_wake_time(self, now: datetime) -> datetime:
        """Return the next scheduler wake time without touching the database.

        Old behavior checked the DB every 30 seconds. That prevents Neon from going idle.
        New behavior wakes only at:
        - daily scan time
        - retry windows after daily time
        - cutoff time
        - tomorrow's daily scan time
        """
        daily_time = _parse_hhmm(self.runner.get_daily_time())
        cutoff_time = _parse_hhmm(self.settings.cutoff_time)
        daily_dt = datetime.combine(now.date(), daily_time, tzinfo=now.tzinfo)
        cutoff_dt = datetime.combine(now.date(), cutoff_time, tzinfo=now.tzinfo)

        if now < daily_dt:
            return daily_dt

        # If cutoff is earlier than daily time, treat cutoff as tomorrow's cutoff.
        if cutoff_dt <= daily_dt:
            cutoff_dt += timedelta(days=1)

        if now < cutoff_dt:
            interval_seconds = max(60, int(self.settings.retry_interval_minutes) * 60)
            elapsed = max(0, (now - daily_dt).total_seconds())
            next_slot_index = max(1, math.floor(elapsed / interval_seconds) + 1)
            next_dt = daily_dt + timedelta(seconds=next_slot_index * interval_seconds)
            if next_dt > cutoff_dt:
                return cutoff_dt
            return next_dt

        return daily_dt + timedelta(days=1)

    async def _sleep_until(self, wake_time: datetime) -> bool:
        """Sleep until wake_time, but remain responsive to shutdown.

        Sleeps in chunks so SIGTERM on Render can stop cleanly.
        Returns False if stopping was requested.
        """
        while not self._stopping.is_set():
            now = datetime.now(self.settings.tzinfo)
            seconds = (wake_time - now).total_seconds()
            if seconds <= 0:
                return True
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=min(seconds, 300))
                return False
            except asyncio.TimeoutError:
                continue
        return False

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            now = datetime.now(self.settings.tzinfo)
            wake_time = self._next_wake_time(now)
            logger.info("Next scheduled DB scan at %s %s", wake_time.strftime("%Y-%m-%d %H:%M:%S"), self.settings.timezone)
            should_run = await self._sleep_until(wake_time)
            if not should_run:
                break

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
