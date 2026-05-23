from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AppSetting, DailyPairProgress, ForwardedPost, Pair, ReportLog, utcnow


class Repository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_pair(
        self,
        *,
        report_username: str,
        source_input: str,
        source_chat_id: int,
        source_title: str | None,
        target_input: str,
        target_chat_id: int,
        target_title: str | None,
        target_username: str | None,
        first_day: int,
        current_day: int,
        next_reset_date: date,
        last_seen_message_id: int,
        post_count: int = 1,
    ) -> Pair:
        pair = Pair(
            report_username=report_username,
            source_input=source_input,
            source_chat_id=source_chat_id,
            source_title=source_title,
            target_input=target_input,
            target_chat_id=target_chat_id,
            target_title=target_title,
            target_username=target_username,
            first_day=first_day,
            current_day=current_day,
            next_reset_date=next_reset_date,
            last_seen_message_id=last_seen_message_id,
            post_count=max(1, post_count),
            pin_mode="all",
        )
        self.session.add(pair)
        await self.session.flush()
        await self.session.refresh(pair)
        return pair

    async def get_pair(self, pair_id: int) -> Pair | None:
        return await self.session.get(Pair, pair_id)

    async def list_pairs(self, *, active_only: bool = False) -> list[Pair]:
        stmt = select(Pair).order_by(Pair.id.asc())
        if active_only:
            stmt = stmt.where(Pair.active.is_(True))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def delete_pair(self, pair_id: int) -> bool:
        result = await self.session.execute(delete(Pair).where(Pair.id == pair_id))
        return bool(result.rowcount)

    async def set_pair_active(self, pair_id: int, active: bool) -> Pair | None:
        pair = await self.get_pair(pair_id)
        if not pair:
            return None
        pair.active = active
        pair.updated_at = utcnow()
        await self.session.flush()
        return pair

    async def update_pair_error(self, pair: Pair, error: str | None) -> None:
        now = utcnow()
        pair.last_error = error
        pair.last_run_at = now
        pair.updated_at = now
        await self.session.flush()

    async def mark_pair_success(
        self,
        pair: Pair,
        *,
        newest_source_message_id: int | None = None,
        current_day: int | None = None,
        next_reset_date: date | None = None,
    ) -> None:
        now = utcnow()
        pair.last_run_at = now
        pair.last_success_at = now
        pair.last_error = None
        pair.updated_at = now
        if newest_source_message_id is not None:
            pair.last_seen_message_id = newest_source_message_id
        if current_day is not None:
            pair.current_day = current_day
        if next_reset_date is not None:
            pair.next_reset_date = next_reset_date
        await self.session.flush()

    async def get_daily_progress(self, pair_id: int, run_date: date) -> DailyPairProgress | None:
        result = await self.session.execute(
            select(DailyPairProgress).where(
                DailyPairProgress.pair_id == pair_id,
                DailyPairProgress.run_date == run_date,
            )
        )
        return result.scalars().first()

    async def get_or_create_daily_progress(
        self,
        *,
        pair: Pair,
        run_date: date,
        day_number: int,
        required_post_count: int,
    ) -> DailyPairProgress:
        progress = await self.get_daily_progress(pair.id, run_date)
        if progress:
            # Keep today's progress tied to the current pair settings. Do not reduce forwarded_count.
            progress.day_number = day_number
            progress.required_post_count = max(1, required_post_count)
            progress.updated_at = utcnow()
            await self.session.flush()
            return progress

        progress = DailyPairProgress(
            pair_id=pair.id,
            run_date=run_date,
            day_number=day_number,
            required_post_count=max(1, required_post_count),
            forwarded_count=0,
        )
        self.session.add(progress)
        await self.session.flush()
        await self.session.refresh(progress)
        return progress

    async def update_daily_progress(
        self,
        progress: DailyPairProgress,
        *,
        checked: bool = False,
        added_forwarded_count: int = 0,
        reported_at: datetime | None = None,
        completed_at: datetime | None = None,
        last_error: str | None = None,
    ) -> None:
        now = utcnow()
        if checked:
            progress.last_checked_at = now
        if added_forwarded_count:
            progress.forwarded_count += added_forwarded_count
            progress.last_forwarded_at = now
        if reported_at is not None:
            progress.reported_at = reported_at
        if completed_at is not None:
            progress.completed_at = completed_at
        progress.last_error = last_error
        progress.updated_at = now
        await self.session.flush()

    async def save_forwarded_post(
        self,
        *,
        pair_id: int,
        run_date: date | None,
        day_number: int | None,
        source_chat_id: int,
        source_message_id: int,
        target_chat_id: int,
        target_message_id: int,
        target_post_link: str,
    ) -> ForwardedPost:
        item = ForwardedPost(
            pair_id=pair_id,
            run_date=run_date,
            day_number=day_number,
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
            target_post_link=target_post_link,
        )
        self.session.add(item)
        await self.session.flush()
        return item

    async def list_daily_post_links(self, *, pair_id: int, run_date: date) -> list[str]:
        result = await self.session.execute(
            select(ForwardedPost.target_post_link)
            .where(ForwardedPost.pair_id == pair_id, ForwardedPost.run_date == run_date)
            .order_by(ForwardedPost.id.asc())
        )
        return [str(row[0]) for row in result.all()]

    async def save_report_log(
        self,
        *,
        pair_id: int,
        day_number: int,
        report_username: str,
        channel_link: str,
        post_links: list[str],
    ) -> ReportLog:
        log = ReportLog(
            pair_id=pair_id,
            day_number=day_number,
            report_username=report_username,
            channel_link=channel_link,
            post_links="\n".join(post_links),
        )
        self.session.add(log)
        await self.session.flush()
        return log

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        item = await self.session.get(AppSetting, key)
        return item.value if item else default

    async def set_setting(self, key: str, value: str) -> None:
        item = await self.session.get(AppSetting, key)
        if item:
            item.value = value
            item.updated_at = utcnow()
        else:
            self.session.add(AppSetting(key=key, value=value))
        await self.session.flush()
