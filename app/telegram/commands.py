from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from telethon import TelegramClient, events
from telethon.events import NewMessage

from app.config import Settings
from app.db.repository import Repository
from app.db.session import Database
from app.services.pair_runner import PairRunner
from app.services.pair_service import PairService
from app.utils.text import is_valid_time_hhmm

logger = logging.getLogger(__name__)

HELP_TEXT = """🧭 Session Pair Userbot Commands

/addpair @username source_link target_link first_day [post_count]
/listpairs
/pair pair_id
/delpair pair_id
/pausepair pair_id
/resumepair pair_id
/runpair pair_id
/runall
/dailyprogress
/edituser pair_id @newusername
/editsource pair_id new_source_link
/edittarget pair_id new_target_link
/resetday pair_id day_number
/setfirstday pair_id first_day
/setpostcount pair_id count
/settime HH:MM
/setpinmode pair_id all|none
/testnotify pair_id
/testforward pair_id
/status
/help

Default post_count = 1.
Daily scan starts at 09:00 Asia/Yangon by default.
Incomplete pairs retry every 1 hour until 23:00 cutoff.
"""


@dataclass(slots=True)
class CommandContext:
    db: Database
    settings: Settings
    pair_service: PairService
    runner: PairRunner
    self_id: int


class CommandRouter:
    def __init__(self, client: TelegramClient, ctx: CommandContext) -> None:
        self.client = client
        self.ctx = ctx
        self.control_peer: int | str | None = ctx.settings.control_group_id if ctx.settings.control_mode == "group" else "me"

    def register(self) -> None:
        self.client.add_event_handler(self._on_message, events.NewMessage())

    async def send_control(self, text: str) -> None:
        await self.client.send_message(self.control_peer, text)

    async def _is_allowed(self, event: NewMessage.Event) -> bool:
        text = event.raw_text or ""
        if not text.startswith("/"):
            return False

        sender_id = int(event.sender_id or 0)
        allowed_admins = set(self.ctx.settings.admin_user_ids)
        allowed_admins.add(self.ctx.self_id)

        if self.ctx.settings.control_mode == "group":
            if int(event.chat_id or 0) != int(self.ctx.settings.control_group_id or 0):
                return False
            return sender_id in allowed_admins

        # Saved Messages mode: only commands in own Saved Messages.
        return int(event.chat_id or 0) == self.ctx.self_id

    async def _reply(self, event: NewMessage.Event, text: str) -> None:
        if len(text) > 3900:
            chunks = [text[i : i + 3900] for i in range(0, len(text), 3900)]
            for chunk in chunks:
                await event.respond(chunk)
            return
        await event.respond(text)

    async def _on_message(self, event: NewMessage.Event) -> None:
        if not await self._is_allowed(event):
            return
        parts = (event.raw_text or "").strip().split()
        if not parts:
            return
        cmd = parts[0].split("@", 1)[0].lower()
        args = parts[1:]
        try:
            if cmd in {"/help", "/start"}:
                await self._reply(event, HELP_TEXT)
            elif cmd == "/status":
                await self._reply(event, await self._status())
            elif cmd == "/addpair":
                await self._reply(event, await self._addpair(args))
            elif cmd == "/listpairs":
                await self._reply(event, await self._listpairs())
            elif cmd == "/pair":
                await self._reply(event, await self._pair(args))
            elif cmd == "/delpair":
                await self._reply(event, await self._delpair(args))
            elif cmd == "/pausepair":
                await self._reply(event, await self._active(args, False))
            elif cmd == "/resumepair":
                await self._reply(event, await self._active(args, True))
            elif cmd == "/runpair":
                await self._reply(event, await self._runpair(args))
            elif cmd == "/runall":
                await self._reply(event, await self._runall())
            elif cmd == "/dailyprogress":
                await self._reply(event, await self._dailyprogress())
            elif cmd == "/edituser":
                await self._reply(event, await self._edituser(args))
            elif cmd == "/editsource":
                await self._reply(event, await self._editsource(args))
            elif cmd == "/edittarget":
                await self._reply(event, await self._edittarget(args))
            elif cmd == "/resetday":
                await self._reply(event, await self._resetday(args))
            elif cmd == "/setfirstday":
                await self._reply(event, await self._setfirstday(args))
            elif cmd == "/setpostcount":
                await self._reply(event, await self._setpostcount(args))
            elif cmd == "/settime":
                await self._reply(event, await self._settime(args))
            elif cmd == "/setpinmode":
                await self._reply(event, await self._setpinmode(args))
            elif cmd == "/testnotify":
                await self._reply(event, await self._testnotify(args))
            elif cmd == "/testforward":
                await self._reply(event, await self._runpair(args))
            else:
                await self._reply(event, "❌ Unknown command. Use /help")
        except Exception as exc:
            logger.exception("Command failed: %s", cmd)
            await self._reply(event, f"❌ {type(exc).__name__}: {exc}")

    def _need_pair_id(self, args: list[str]) -> int:
        if not args or not args[0].isdigit():
            raise ValueError("pair_id is required.")
        return int(args[0])

    async def _status(self) -> str:
        async with self.ctx.db.session() as session:
            repo = Repository(session)
            pairs = await repo.list_pairs()
            daily_time = await repo.get_setting("daily_run_time", self.ctx.settings.daily_run_time)
        active = sum(1 for p in pairs if p.active)
        return (
            "✅ Userbot is running\n\n"
            f"Control mode: {self.ctx.settings.control_mode}\n"
            f"Timezone: {self.ctx.settings.timezone}\n"
            f"Daily scan time: {daily_time}\n"
            f"Retry interval: {self.ctx.settings.retry_interval_minutes} minute(s)\n"
            f"Cutoff time: {self.ctx.settings.cutoff_time}\n"
            f"Job delay: {self.ctx.settings.job_delay_seconds} second(s)\n"
            f"Pairs: {len(pairs)} total / {active} active"
        )

    async def _addpair(self, args: list[str]) -> str:
        if len(args) not in {4, 5}:
            return "❌ Usage: /addpair @username source_link target_link first_day [post_count]"
        username, source, target, first_day_raw = args[:4]
        post_count_raw = args[4] if len(args) == 5 else "1"
        if not first_day_raw.isdigit():
            return "❌ first_day must be a number, example 3."
        if not post_count_raw.isdigit():
            return "❌ post_count must be a number, example 2."
        return await self.ctx.pair_service.add_pair(
            username=username,
            source=source,
            target=target,
            first_day=int(first_day_raw),
            post_count=int(post_count_raw),
        )

    async def _listpairs(self) -> str:
        async with self.ctx.db.session() as session:
            repo = Repository(session)
            pairs = await repo.list_pairs()
        if not pairs:
            return "No pairs yet."
        lines = ["📋 Pairs"]
        for pair in pairs:
            status = "active" if pair.active else "paused"
            lines.append(
                f"\n#{pair.id} [{status}] Day{pair.current_day} | post_count={pair.post_count}\n"
                f"User: {pair.report_username}\n"
                f"Source: {pair.source_input}\n"
                f"Target: {pair.target_input}\n"
                f"Next reset: {pair.next_reset_date}\n"
                f"Last seen: {pair.last_seen_message_id}"
            )
        return "\n".join(lines)

    async def _pair(self, args: list[str]) -> str:
        pair_id = self._need_pair_id(args)
        today = datetime.now(self.ctx.settings.tzinfo).date()
        async with self.ctx.db.session() as session:
            repo = Repository(session)
            pair = await repo.get_pair(pair_id)
            progress = await repo.get_daily_progress(pair_id, today)
        if not pair:
            return "❌ Pair not found."
        status = "active" if pair.active else "paused"
        progress_line = "No daily progress yet today."
        if progress:
            progress_line = (
                f"Today progress: {progress.forwarded_count}/{progress.required_post_count}\n"
                f"Reported: {'yes' if progress.reported_at else 'no'}\n"
                f"Completed: {'yes' if progress.completed_at else 'no'}"
            )
        return (
            f"Pair #{pair.id} [{status}]\n\n"
            f"User: {pair.report_username}\n"
            f"Source: {pair.source_input}\n"
            f"Source ID: {pair.source_chat_id}\n"
            f"Target: {pair.target_input}\n"
            f"Target ID: {pair.target_chat_id}\n"
            f"First day: {pair.first_day}\n"
            f"Current day: {pair.current_day}\n"
            f"Post count: {pair.post_count}\n"
            f"Next reset: {pair.next_reset_date}\n"
            f"Last seen source post: {pair.last_seen_message_id}\n"
            f"Pin mode: {pair.pin_mode}\n"
            f"Last run: {pair.last_run_at}\n"
            f"Last success: {pair.last_success_at}\n"
            f"Last error: {pair.last_error or '-'}\n\n"
            f"{progress_line}"
        )

    async def _dailyprogress(self) -> str:
        today = datetime.now(self.ctx.settings.tzinfo).date()
        async with self.ctx.db.session() as session:
            repo = Repository(session)
            pairs = await repo.list_pairs(active_only=True)
            rows = []
            for pair in pairs:
                progress = await repo.get_daily_progress(pair.id, today)
                if progress:
                    rows.append(
                        f"#{pair.id}: {progress.forwarded_count}/{progress.required_post_count} "
                        f"reported={'yes' if progress.reported_at else 'no'} "
                        f"completed={'yes' if progress.completed_at else 'no'}"
                    )
                else:
                    rows.append(f"#{pair.id}: 0/{pair.post_count} not started")
        if not rows:
            return "No active pairs."
        return "📅 Today daily progress\n" + "\n".join(rows)

    async def _delpair(self, args: list[str]) -> str:
        pair_id = self._need_pair_id(args)
        async with self.ctx.db.session() as session:
            repo = Repository(session)
            ok = await repo.delete_pair(pair_id)
        return f"✅ Pair {pair_id} deleted." if ok else "❌ Pair not found."

    async def _active(self, args: list[str], active: bool) -> str:
        pair_id = self._need_pair_id(args)
        async with self.ctx.db.session() as session:
            repo = Repository(session)
            pair = await repo.set_pair_active(pair_id, active)
        if not pair:
            return "❌ Pair not found."
        return f"✅ Pair {pair_id} {'resumed' if active else 'paused'}."

    async def _runpair(self, args: list[str]) -> str:
        pair_id = self._need_pair_id(args)
        result = await self.ctx.runner.run_pair(pair_id, force=True)
        lines = ["✅" if result.ok else "❌", f"Pair {pair_id}: {result.message}"]
        if result.post_links:
            lines.append("\n".join(result.post_links))
        return "\n".join(lines)

    async def _runall(self) -> str:
        results = await self.ctx.runner.run_all()
        if not results:
            return "No active pairs."
        lines = ["▶️ Run all finished"]
        for result in results:
            mark = "✅" if result.ok else "❌"
            lines.append(f"{mark} Pair {result.pair_id}: {result.message}")
        return "\n".join(lines)

    async def _edituser(self, args: list[str]) -> str:
        pair_id = self._need_pair_id(args)
        if len(args) < 2:
            return "❌ Usage: /edituser pair_id @newusername"
        return await self.ctx.pair_service.edit_user(pair_id, args[1])

    async def _editsource(self, args: list[str]) -> str:
        pair_id = self._need_pair_id(args)
        if len(args) < 2:
            return "❌ Usage: /editsource pair_id new_source_link"
        return await self.ctx.pair_service.edit_source(pair_id, args[1])

    async def _edittarget(self, args: list[str]) -> str:
        pair_id = self._need_pair_id(args)
        if len(args) < 2:
            return "❌ Usage: /edittarget pair_id new_target_link"
        return await self.ctx.pair_service.edit_target(pair_id, args[1])

    async def _resetday(self, args: list[str]) -> str:
        pair_id = self._need_pair_id(args)
        if len(args) < 2 or not args[1].isdigit():
            return "❌ Usage: /resetday pair_id day_number"
        return await self.ctx.pair_service.reset_day(pair_id, int(args[1]))

    async def _setfirstday(self, args: list[str]) -> str:
        pair_id = self._need_pair_id(args)
        if len(args) < 2 or not args[1].isdigit():
            return "❌ Usage: /setfirstday pair_id first_day"
        return await self.ctx.pair_service.set_first_day(pair_id, int(args[1]))

    async def _setpostcount(self, args: list[str]) -> str:
        pair_id = self._need_pair_id(args)
        if len(args) < 2 or not args[1].isdigit():
            return "❌ Usage: /setpostcount pair_id count"
        return await self.ctx.pair_service.set_post_count(pair_id, int(args[1]))

    async def _settime(self, args: list[str]) -> str:
        if len(args) != 1 or not is_valid_time_hhmm(args[0]):
            return "❌ Usage: /settime HH:MM"
        async with self.ctx.db.session() as session:
            repo = Repository(session)
            await repo.set_setting("daily_run_time", args[0])
        return f"✅ Daily scan time set to {args[0]} ({self.ctx.settings.timezone})."

    async def _setpinmode(self, args: list[str]) -> str:
        pair_id = self._need_pair_id(args)
        if len(args) < 2:
            return "❌ Usage: /setpinmode pair_id all|none"
        return await self.ctx.pair_service.set_pin_mode(pair_id, args[1])

    async def _testnotify(self, args: list[str]) -> str:
        pair_id = self._need_pair_id(args)
        async with self.ctx.db.session() as session:
            repo = Repository(session)
            pair = await repo.get_pair(pair_id)
        if not pair:
            return "❌ Pair not found."
        text = f"Test notification for Pair {pair.id}\nDay{pair.current_day}\n{pair.target_input}"
        await self.client.send_message(pair.report_username, text)
        return f"✅ Test notification sent to {pair.report_username}."
