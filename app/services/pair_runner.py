from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, time, timezone

from app.config import Settings
from app.db.models import DailyPairProgress, Pair, utcnow
from app.db.repository import Repository
from app.db.session import Database
from app.services.report_service import build_report_message
from app.services.telegram_service import TelegramService
from app.utils.dates import apply_monthly_reset

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PairRunResult:
    pair_id: int
    ok: bool
    message: str
    post_links: list[str]


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(hour=int(hour), minute=int(minute))


def _ensure_aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class PairRunner:
    def __init__(self, *, db: Database, telegram: TelegramService, settings: Settings) -> None:
        self.db = db
        self.telegram = telegram
        self.settings = settings

    async def _delay(self) -> None:
        if self.settings.job_delay_seconds > 0:
            await asyncio.sleep(self.settings.job_delay_seconds)

    async def run_all(self) -> list[PairRunResult]:
        async with self.db.session() as session:
            repo = Repository(session)
            pairs = await repo.list_pairs(active_only=True)
        return await self._run_pair_ids([pair.id for pair in pairs], force=True)

    async def run_due(self) -> list[PairRunResult]:
        """Run only pairs that are due today.

        Due rules:
        - Start scanning at DAILY_RUN_TIME.
        - If a pair has no posts or not enough posts, retry it every RETRY_INTERVAL_MINUTES.
        - At CUTOFF_TIME, send a partial report if at least one post was forwarded.
        - Once a pair is completed for today's local date, do not scan it again until tomorrow.
        """
        now_local = datetime.now(self.settings.tzinfo)
        daily_time = await self._daily_time()
        if now_local.time() < _parse_hhmm(daily_time):
            return []

        cutoff_reached = now_local.time() >= _parse_hhmm(self.settings.cutoff_time)
        run_date = now_local.date()
        now_utc = datetime.now(timezone.utc)
        due_pair_ids: list[int] = []

        async with self.db.session() as session:
            repo = Repository(session)
            pairs = await repo.list_pairs(active_only=True)
            for pair in pairs:
                progress = await repo.get_daily_progress(pair.id, run_date)
                if progress and progress.completed_at:
                    continue
                if progress is None:
                    due_pair_ids.append(pair.id)
                    continue
                if progress.forwarded_count >= max(1, pair.post_count):
                    due_pair_ids.append(pair.id)
                    continue
                if cutoff_reached:
                    due_pair_ids.append(pair.id)
                    continue
                last_checked = _ensure_aware_utc(progress.last_checked_at)
                if last_checked is None:
                    due_pair_ids.append(pair.id)
                    continue
                age_seconds = (now_utc - last_checked).total_seconds()
                if age_seconds >= self.settings.retry_interval_minutes * 60:
                    due_pair_ids.append(pair.id)

        return await self._run_pair_ids(due_pair_ids, force=False)

    async def _daily_time(self) -> str:
        async with self.db.session() as session:
            repo = Repository(session)
            return await repo.get_setting("daily_run_time", self.settings.daily_run_time) or self.settings.daily_run_time

    async def _run_pair_ids(self, pair_ids: list[int], *, force: bool) -> list[PairRunResult]:
        results: list[PairRunResult] = []
        for index, pair_id in enumerate(pair_ids):
            results.append(await self.run_pair(pair_id, force=force))
            if index < len(pair_ids) - 1:
                await self._delay()
        return results

    async def run_pair(self, pair_id: int, *, force: bool = True) -> PairRunResult:
        now_local = datetime.now(self.settings.tzinfo)
        today = now_local.date()
        cutoff_reached = now_local.time() >= _parse_hhmm(self.settings.cutoff_time)

        async with self.db.session() as session:
            repo = Repository(session)
            pair = await repo.get_pair(pair_id)
            if not pair:
                return PairRunResult(pair_id, False, "Pair not found.", [])
            if not pair.active:
                return PairRunResult(pair_id, False, "Pair is paused.", [])

            try:
                day_number, next_reset, _ = apply_monthly_reset(today, pair.current_day, pair.next_reset_date)
                required = max(1, int(pair.post_count or 1))
                progress = await repo.get_or_create_daily_progress(
                    pair=pair,
                    run_date=today,
                    day_number=day_number,
                    required_post_count=required,
                )

                if progress.completed_at and not force:
                    return PairRunResult(pair.id, True, "Today's quota is already completed. Skipped until tomorrow.", [])
                if progress.reported_at:
                    return PairRunResult(pair.id, True, "Today's report already sent. Skipped until tomorrow.", [])

                await repo.update_daily_progress(progress, checked=True)
                remaining = max(required - progress.forwarded_count, 0)
                new_links: list[str] = []
                newest_selected_source_id: int | None = None

                if remaining > 0 and (force or not progress.completed_at):
                    messages = await self.telegram.iter_new_messages(
                        pair.source_input,
                        after_id=pair.last_seen_message_id,
                        limit=self.settings.scan_limit_per_pair,
                    )
                    selected = messages[:remaining]

                    if selected:
                        forward_results = await self.telegram.forward_messages(
                            source_chat=pair.source_input,
                            target_chat=pair.target_input,
                            source_messages=selected,
                            target_chat_id=pair.target_chat_id,
                            target_username=pair.target_username,
                            target_input=pair.target_input,
                        )

                        for result in forward_results:
                            await repo.save_forwarded_post(
                                pair_id=pair.id,
                                run_date=today,
                                day_number=day_number,
                                source_chat_id=pair.source_chat_id,
                                source_message_id=result.source_message_id,
                                target_chat_id=pair.target_chat_id,
                                target_message_id=result.target_message_id,
                                target_post_link=result.target_post_link,
                            )

                        new_links = [item.target_post_link for item in forward_results]
                        if forward_results:
                            newest_selected_source_id = max(item.source_message_id for item in forward_results)
                            await repo.update_daily_progress(
                                progress,
                                added_forwarded_count=len(forward_results),
                            )
                            await repo.mark_pair_success(
                                pair,
                                newest_source_message_id=newest_selected_source_id,
                                current_day=day_number,
                                next_reset_date=next_reset,
                            )

                            if pair.pin_mode in {"all", "last"}:
                                to_pin = forward_results if pair.pin_mode == "all" else [forward_results[-1]]
                                for item in to_pin:
                                    await self.telegram.pin_message(pair.target_input, item.target_message_id)

                all_links = await repo.list_daily_post_links(pair_id=pair.id, run_date=today)
                forwarded_count = len(all_links)
                quota_met = forwarded_count >= required

                if quota_met or (cutoff_reached and forwarded_count > 0):
                    await self._delay()
                    report_text = build_report_message(
                        day_number=day_number,
                        channel_link=pair.target_input,
                        post_links=all_links,
                    )
                    await self.telegram.send_report(pair.report_username, report_text)
                    now = utcnow()
                    await repo.save_report_log(
                        pair_id=pair.id,
                        day_number=day_number,
                        report_username=pair.report_username,
                        channel_link=pair.target_input,
                        post_links=all_links,
                    )
                    await repo.update_daily_progress(progress, reported_at=now, completed_at=now)
                    await repo.mark_pair_success(
                        pair,
                        newest_source_message_id=newest_selected_source_id,
                        current_day=day_number + 1,
                        next_reset_date=next_reset,
                    )
                    reason = "quota met" if quota_met else f"{self.settings.cutoff_time} cutoff partial report"
                    return PairRunResult(
                        pair.id,
                        True,
                        f"Forwarded/reported {forwarded_count}/{required} post(s) for Day{day_number} ({reason}).",
                        all_links,
                    )

                if cutoff_reached and forwarded_count == 0:
                    now = utcnow()
                    await repo.update_daily_progress(progress, completed_at=now)
                    await repo.mark_pair_success(pair, current_day=day_number, next_reset_date=next_reset)
                    return PairRunResult(
                        pair.id,
                        True,
                        f"No posts found by {self.settings.cutoff_time}. Pair completed for today without report. Day remains Day{day_number}.",
                        [],
                    )

                await repo.mark_pair_success(
                    pair,
                    newest_source_message_id=newest_selected_source_id,
                    current_day=day_number,
                    next_reset_date=next_reset,
                )
                if new_links:
                    return PairRunResult(
                        pair.id,
                        True,
                        f"Forwarded {forwarded_count}/{required} post(s). Waiting for {required - forwarded_count} more before report.",
                        all_links,
                    )
                return PairRunResult(
                    pair.id,
                    True,
                    f"No new posts. Waiting for {required - forwarded_count} more. Next retry in {self.settings.retry_interval_minutes} minute(s).",
                    all_links,
                )
            except Exception as exc:
                logger.exception("Pair %s failed", pair_id)
                try:
                    progress = await repo.get_daily_progress(pair.id, today)
                    if progress:
                        await repo.update_daily_progress(progress, last_error=f"{type(exc).__name__}: {exc}")
                except Exception:
                    logger.exception("Failed to update daily progress error for pair %s", pair_id)
                await repo.update_pair_error(pair, f"{type(exc).__name__}: {exc}")
                return PairRunResult(pair_id, False, f"{type(exc).__name__}: {exc}", [])
