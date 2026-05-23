from __future__ import annotations

from datetime import datetime

from app.config import Settings
from app.db.repository import Repository
from app.db.session import Database
from app.services.telegram_service import TelegramService
from app.utils.dates import initial_next_reset_date
from app.utils.text import normalize_username


class PairService:
    def __init__(self, *, db: Database, telegram: TelegramService, settings: Settings) -> None:
        self.db = db
        self.telegram = telegram
        self.settings = settings

    async def add_pair(self, *, username: str, source: str, target: str, first_day: int, post_count: int = 1) -> str:
        if first_day < 1 or first_day > 31:
            raise ValueError("first_day must be between 1 and 31")
        if post_count < 1 or post_count > 50:
            raise ValueError("post_count must be between 1 and 50")

        report_username = normalize_username(username)
        source_info = await self.telegram.resolve_chat(source)
        target_info = await self.telegram.resolve_chat(target)
        await self.telegram.assert_target_admin(target)
        latest_id = await self.telegram.latest_message_id(source)

        today = datetime.now(self.settings.tzinfo).date()
        next_reset = initial_next_reset_date(today, first_day)

        async with self.db.session() as session:
            repo = Repository(session)
            pair = await repo.create_pair(
                report_username=report_username,
                source_input=source,
                source_chat_id=source_info.chat_id,
                source_title=source_info.title,
                target_input=target_info.link,
                target_chat_id=target_info.chat_id,
                target_title=target_info.title,
                target_username=target_info.username,
                first_day=first_day,
                current_day=first_day,
                next_reset_date=next_reset,
                last_seen_message_id=latest_id,
                post_count=post_count,
            )
            return (
                "✅ Pair added\n\n"
                f"Pair ID: {pair.id}\n"
                f"User: {pair.report_username}\n"
                f"Source: {source}\n"
                f"Target: {target_info.link}\n"
                f"Start Day: Day{first_day}\n"
                f"Daily post count: {post_count}\n"
                f"Next reset: {next_reset.isoformat()}\n"
                f"Last seen source post: {latest_id}"
            )

    async def edit_source(self, pair_id: int, new_source: str) -> str:
        source_info = await self.telegram.resolve_chat(new_source)
        latest_id = await self.telegram.latest_message_id(new_source)
        async with self.db.session() as session:
            repo = Repository(session)
            pair = await repo.get_pair(pair_id)
            if not pair:
                return "❌ Pair not found."
            pair.source_input = new_source
            pair.source_chat_id = source_info.chat_id
            pair.source_title = source_info.title
            pair.last_seen_message_id = latest_id
            return f"✅ Pair {pair_id} source updated. New posts will start after message ID {latest_id}."

    async def edit_target(self, pair_id: int, new_target: str) -> str:
        target_info = await self.telegram.resolve_chat(new_target)
        await self.telegram.assert_target_admin(new_target)
        async with self.db.session() as session:
            repo = Repository(session)
            pair = await repo.get_pair(pair_id)
            if not pair:
                return "❌ Pair not found."
            pair.target_input = target_info.link
            pair.target_chat_id = target_info.chat_id
            pair.target_title = target_info.title
            pair.target_username = target_info.username
            return f"✅ Pair {pair_id} target updated: {target_info.link}"

    async def edit_user(self, pair_id: int, new_username: str) -> str:
        username = normalize_username(new_username)
        async with self.db.session() as session:
            repo = Repository(session)
            pair = await repo.get_pair(pair_id)
            if not pair:
                return "❌ Pair not found."
            pair.report_username = username
            return f"✅ Pair {pair_id} report user updated: {username}"

    async def reset_day(self, pair_id: int, day: int) -> str:
        if day < 1 or day > 31:
            return "❌ Day must be between 1 and 31."
        async with self.db.session() as session:
            repo = Repository(session)
            pair = await repo.get_pair(pair_id)
            if not pair:
                return "❌ Pair not found."
            pair.current_day = day
            return f"✅ Pair {pair_id} current day set to Day{day}."

    async def set_first_day(self, pair_id: int, first_day: int) -> str:
        if first_day < 1 or first_day > 31:
            return "❌ first_day must be between 1 and 31."
        today = datetime.now(self.settings.tzinfo).date()
        next_reset = initial_next_reset_date(today, first_day)
        async with self.db.session() as session:
            repo = Repository(session)
            pair = await repo.get_pair(pair_id)
            if not pair:
                return "❌ Pair not found."
            pair.first_day = first_day
            pair.current_day = first_day
            pair.next_reset_date = next_reset
            return f"✅ Pair {pair_id} first day reset to Day{first_day}. Next reset: {next_reset.isoformat()}"

    async def set_post_count(self, pair_id: int, post_count: int) -> str:
        if post_count < 1 or post_count > 50:
            return "❌ post_count must be between 1 and 50."
        async with self.db.session() as session:
            repo = Repository(session)
            pair = await repo.get_pair(pair_id)
            if not pair:
                return "❌ Pair not found."
            pair.post_count = post_count
            return f"✅ Pair {pair_id} daily post count set to {post_count}."

    async def set_pin_mode(self, pair_id: int, pin_mode: str) -> str:
        pin_mode = pin_mode.lower().strip()
        if pin_mode not in {"all", "last", "none"}:
            return "❌ pin_mode must be all, last, or none."
        async with self.db.session() as session:
            repo = Repository(session)
            pair = await repo.get_pair(pair_id)
            if not pair:
                return "❌ Pair not found."
            pair.pin_mode = pin_mode
            return f"✅ Pair {pair_id} pin mode set to {pin_mode}."
