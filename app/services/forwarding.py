from __future__ import annotations
import asyncio, logging
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from app.config import Settings
from app.db.session import Database
from app.db.models import AppUser, Pair, DailyPairProgress, ForwardedPost, ReportLog, PairReportOutbox, AppLog, SmartReport
from app.services.telegram_gateway import TelegramGateway
from app.utils import apply_monthly_reset, channel_link_for_chat, channel_post_link, initial_next_reset_date, report_retry_delay
logger = logging.getLogger(__name__)

def local_now(settings: Settings): return datetime.now(settings.tzinfo)
def parse_hhmm(value: str):
    h, m = map(int, value.split(":")); return h, m

def at_or_after(now: datetime, value: str) -> bool:
    h, m = parse_hhmm(value); return (now.hour, now.minute) >= (h, m)

class ForwardingEngine:
    def __init__(self, db: Database, telegram: TelegramGateway, bot, settings: Settings):
        self.db, self.telegram, self.bot, self.settings = db, telegram, bot, settings

    async def assign_legacy_pairs(self, fallback_telegram_id: int | None):
        if not fallback_telegram_id: return
        async with self.db.session() as session:
            rows = (await session.scalars(select(Pair).where(Pair.owner_user_id.is_(None)))).all()
            if not rows: return
            user = await session.scalar(select(AppUser).where(AppUser.telegram_id == fallback_telegram_id))
            if not user:
                user = AppUser(telegram_id=fallback_telegram_id, is_admin=fallback_telegram_id in self.settings.admin_user_ids)
                session.add(user); await session.flush()
            elif fallback_telegram_id in self.settings.admin_user_ids:
                user.is_admin = True
            for pair in rows: pair.owner_user_id = user.id
            logger.info("Assigned %d legacy pair(s) to configured owner ID %s", len(rows), fallback_telegram_id)

    async def validate_channels(self, source: str, target: str):
        source_info = await self.telegram.resolve_chat(source)
        target_info = await self.telegram.resolve_chat(target)
        if int(source_info.chat_id) == int(target_info.chat_id):
            raise ValueError("Source and target channels must be different")
        await self.telegram.assert_target_admin(target)
        latest = await self.telegram.latest_message_id(source)
        return source_info, target_info, latest

    async def tick(self):
        """Select due pairs in bulk; Report User delivery is pumped after Smart Reporting by the scheduler."""
        if not self.telegram.connected: return
        now = local_now(self.settings); today = now.date()
        async with self.db.session() as session:
            pairs = (await session.scalars(select(Pair).where(Pair.active.is_(True)).order_by(Pair.id))).all()
            pair_ids = [pair.id for pair in pairs]
            progresses = (await session.scalars(select(DailyPairProgress).where(
                DailyPairProgress.run_date == today, DailyPairProgress.pair_id.in_(pair_ids)
            ))).all() if pair_ids else []
            progress_map = {progress.pair_id: progress for progress in progresses}
            due: list[tuple[dict, bool]] = []
            now_utc = datetime.now(timezone.utc)
            for pair in pairs:
                start_time = pair.start_time or self.settings.daily_run_time
                cutoff_time = pair.cutoff_time or self.settings.cutoff_time
                if not at_or_after(now, start_time): continue
                progress = progress_map.get(pair.id)
                if progress and progress.completed_at: continue
                cutoff_reached = at_or_after(now, cutoff_time)
                # Newly-created pairs scan once immediately. Existing incomplete pairs retry at the
                # configured interval. Cutoff is processed once when the last check happened before it;
                # this avoids retrying a failed report every 120-second scheduler tick all night.
                is_due = progress is None
                if progress is not None:
                    last = progress.last_checked_at
                    if last is None:
                        is_due = True
                    else:
                        if last.tzinfo is None: last = last.replace(tzinfo=timezone.utc)
                        age_seconds = (now_utc - last.astimezone(timezone.utc)).total_seconds()
                        checked_local = last.astimezone(self.settings.tzinfo)
                        checked_after_cutoff = at_or_after(checked_local, cutoff_time) and checked_local.date() == today
                        cutoff_needs_one_check = cutoff_reached and not checked_after_cutoff
                        retry_due = age_seconds >= self.settings.retry_interval_minutes * 60
                        is_due = cutoff_needs_one_check or retry_due
                if is_due:
                    snapshot = {col: getattr(pair, col) for col in (
                        "id", "owner_user_id", "pair_name", "report_username", "source_input", "source_chat_id",
                        "target_input", "target_chat_id", "target_username", "current_day", "first_day",
                        "next_reset_date", "post_count", "last_seen_message_id", "pin_mode", "pin_count")}
                    due.append((snapshot, cutoff_reached))
        for index, (snapshot, cutoff_reached) in enumerate(due):
            try:
                await self.run_pair(snapshot, today, cutoff_reached)
            except asyncio.CancelledError: raise
            except Exception as exc:
                logger.exception("Pair %s tick failed", snapshot["id"])
                await self.log_db("pair_worker_error", f"pair={snapshot['id']}: {type(exc).__name__}: {exc}", "ERROR")
            if self.settings.job_delay_seconds and index < len(due) - 1:
                await asyncio.sleep(self.settings.job_delay_seconds)
    async def retry_pending_pair_reports(self):
        """Deliver at most five Report User messages per tick so this queue cannot starve Smart Reporting."""
        if not self.telegram.connected:
            return
        async with self.db.session() as session:
            rows = (await session.execute(select(
                PairReportOutbox.id, PairReportOutbox.pair_id, PairReportOutbox.report_username,
                PairReportOutbox.last_attempt_at, PairReportOutbox.attempt_count
            ).where(PairReportOutbox.status == "pending")
             .order_by(PairReportOutbox.report_date, PairReportOutbox.id).limit(5))).all()
        now = datetime.now(timezone.utc)
        for report_id, pair_id, recipient, last_attempt, attempt_count in rows:
            if last_attempt is not None:
                if last_attempt.tzinfo is None: last_attempt = last_attempt.replace(tzinfo=timezone.utc)
                retry_at = last_attempt.astimezone(timezone.utc) + timedelta(
                    seconds=report_retry_delay(self.settings.report_check_interval_seconds, attempt_count))
                if now < retry_at:
                    continue
            async with self.db.session() as session:
                report = await session.get(PairReportOutbox, report_id)
                if not report or report.status != "pending":
                    continue
                if report.last_attempt_at is not None:
                    last = report.last_attempt_at
                    if last.tzinfo is None: last = last.replace(tzinfo=timezone.utc)
                    retry_at = last.astimezone(timezone.utc) + timedelta(
                        seconds=report_retry_delay(self.settings.report_check_interval_seconds, report.attempt_count))
                    if now < retry_at:
                        continue
                report.last_attempt_at = now
                report.attempt_count = int(report.attempt_count or 0) + 1
                recipient = report.report_username
                body = report.body
                report_date = report.report_date
                day_number = report.day_number
                channel_link = report.channel_link
                post_links = report.post_links
            try:
                if self.settings.job_delay_seconds:
                    await asyncio.sleep(self.settings.job_delay_seconds)
                await self.telegram.send_pair_report(recipient, body)
                sent_at = datetime.now(timezone.utc)
                async with self.db.session() as session:
                    report = await session.get(PairReportOutbox, report_id)
                    if report and report.status == "pending":
                        report.status = "sent"
                        report.sent_at = sent_at
                        session.add(ReportLog(pair_id=pair_id, day_number=day_number, report_username=recipient,
                                              channel_link=channel_link, post_links=post_links, sent_at=sent_at))
                    pair = await session.get(Pair, pair_id)
                    progress = await session.scalar(select(DailyPairProgress).where(
                        DailyPairProgress.pair_id == pair_id, DailyPairProgress.run_date == report_date))
                    if pair and pair.last_error and pair.last_error.startswith("Report User delivery failed:"):
                        pair.last_error = None
                    if progress:
                        progress.reported_at = sent_at
                        if progress.last_error and progress.last_error.startswith("Report User delivery failed:"):
                            progress.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                detail = f"Report User delivery failed: {type(exc).__name__}: {exc}"[:1000]
                logger.warning("Report User delivery failed for pair %s: %s", pair_id, exc)
                async with self.db.session() as session:
                    pair = await session.get(Pair, pair_id)
                    if pair: pair.last_error = detail
                    progress = await session.scalar(select(DailyPairProgress).where(
                        DailyPairProgress.pair_id == pair_id, DailyPairProgress.run_date == report_date))
                    if progress: progress.last_error = detail
                    session.add(AppLog(level="ERROR", event="pair_report_failed", detail=f"pair={pair_id}, user={recipient}: {exc}"[:500]))


    async def next_wakeup_seconds(self) -> int:
        """Sleep until the next operational event instead of polling Neon every two minutes."""
        now = local_now(self.settings)
        today = now.date()
        current_time = now.strftime("%H:%M")
        candidates: list[datetime] = []
        async with self.db.session() as session:
            pairs = (await session.scalars(select(Pair).where(Pair.active.is_(True)))).all()
            pair_ids = [pair.id for pair in pairs]
            progresses = (await session.scalars(select(DailyPairProgress).where(
                DailyPairProgress.run_date == today, DailyPairProgress.pair_id.in_(pair_ids)
            ))).all() if pair_ids else []
            progress_map = {progress.pair_id: progress for progress in progresses}
            pending_reports = []
            if self.bot is not None:
                pending_reports = (await session.execute(select(
                    SmartReport.last_attempt_at, SmartReport.attempt_count
                ).where(SmartReport.status == "pending"))).all()
            pending_pair_reports = (await session.execute(select(
                PairReportOutbox.last_attempt_at, PairReportOutbox.attempt_count
            ).where(PairReportOutbox.status == "pending"))).all()
        for pair in pairs:
            start_time = pair.start_time or self.settings.daily_run_time
            cutoff_time = pair.cutoff_time or self.settings.cutoff_time
            start_dt = datetime.combine(today, datetime.strptime(start_time, "%H:%M").time(), tzinfo=self.settings.tzinfo)
            progress = progress_map.get(pair.id)
            if progress and progress.completed_at:
                candidates.append(datetime.combine(today + timedelta(days=1),
                    datetime.strptime(start_time, "%H:%M").time(), tzinfo=self.settings.tzinfo))
                continue
            if current_time < start_time:
                candidates.append(start_dt)
                continue
            if progress is None:
                # A due pair should have been handled in the just-completed tick. This short delay
                # is a fallback for a transient runner error; pair create/update also wakes the loop.
                candidates.append(now + timedelta(seconds=30))
                continue
            last = progress.last_checked_at
            if last is None:
                candidates.append(now + timedelta(seconds=30))
                continue
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            last_local = last.astimezone(self.settings.tzinfo)
            retry_at = last_local + timedelta(minutes=self.settings.retry_interval_minutes)
            cutoff_dt = datetime.combine(today, datetime.strptime(cutoff_time, "%H:%M").time(), tzinfo=self.settings.tzinfo)
            # Ensure one run at cutoff, but after it fails use the normal retry interval.
            if now < cutoff_dt and last_local < cutoff_dt:
                candidates.append(min(retry_at, cutoff_dt))
            else:
                candidates.append(max(retry_at, now + timedelta(seconds=5)))
        for last_attempt, attempt_count in pending_reports + pending_pair_reports:
            if last_attempt is None:
                candidates.append(now + timedelta(seconds=30))
            else:
                if last_attempt.tzinfo is None: last_attempt = last_attempt.replace(tzinfo=timezone.utc)
                retry_at = last_attempt.astimezone(self.settings.tzinfo) + timedelta(
                    seconds=report_retry_delay(self.settings.report_check_interval_seconds, attempt_count))
                candidates.append(max(retry_at, now + timedelta(seconds=1)))
        if not candidates:
            return 12 * 60 * 60
        delay = min((candidate - now).total_seconds() for candidate in candidates)
        return max(1, min(int(delay), 24 * 60 * 60))

    async def run_pair(self, p: dict, today, cutoff_reached: bool):
        pair_id = p["id"]
        now_utc = datetime.now(timezone.utc)
        async with self.db.session() as session:
            pair = await session.get(Pair, pair_id)
            if not pair or not pair.active: return
            day_no, next_reset, did_reset = apply_monthly_reset(today, pair.current_day, pair.next_reset_date)
            progress = await session.scalar(select(DailyPairProgress).where(
                DailyPairProgress.pair_id == pair_id, DailyPairProgress.run_date == today))
            if not progress:
                progress = DailyPairProgress(pair_id=pair_id, run_date=today, day_number=day_no,
                                             required_post_count=max(1, pair.post_count), forwarded_count=0)
                session.add(progress); await session.flush()
            else:
                progress.day_number = day_no
                progress.required_post_count = max(1, pair.post_count)
            if progress.completed_at: return
            progress.last_checked_at = now_utc
            pair.last_run_at = now_utc
            pair.current_day = day_no
            pair.next_reset_date = next_reset
            p["current_day"] = day_no
            after_id = int(pair.last_seen_message_id or 0)
            source_chat_id, target_chat_id = int(pair.source_chat_id), int(pair.target_chat_id)
            required = max(1, int(pair.post_count or 1))
            already = int(progress.forwarded_count or 0)
            remaining = max(0, required - already)
        new_ids: list[tuple[int, int]] = []
        if remaining:
            messages = await self.telegram.iter_new_messages(source_chat_id, after_id, self.settings.scan_limit_per_pair)
            message_ids = [int(message.id) for message in messages if getattr(message, "id", None)]
            async with self.db.session() as session:
                existing_ids = set((await session.scalars(select(ForwardedPost.source_message_id).where(
                    ForwardedPost.pair_id == pair_id,
                    ForwardedPost.source_chat_id == source_chat_id,
                    ForwardedPost.source_message_id.in_(message_ids),
                ))).all()) if message_ids else set()
            forwarded_new = 0
            for message in messages:
                message_id = int(message.id)
                if message_id in existing_ids:
                    # Recovery for a checkpoint that lagged behind a committed history record.
                    # Check before Telegram send, otherwise a retry can duplicate the target post.
                    async with self.db.session() as session:
                        pair = await session.get(Pair, pair_id)
                        if pair: pair.last_seen_message_id = max(pair.last_seen_message_id, message_id)
                    continue
                if forwarded_new >= remaining:
                    break
                try:
                    target_message_id = await self.telegram.forward_one(source_chat_id, target_chat_id, message)
                    link = channel_post_link(target_chat_id, p["target_username"], target_message_id)
                    async with self.db.session() as session:
                        pair = await session.get(Pair, pair_id)
                        if not pair: break
                        post = ForwardedPost(pair_id=pair_id, run_date=today, day_number=p["current_day"],
                                             source_chat_id=source_chat_id, source_message_id=message_id,
                                             target_chat_id=target_chat_id, target_message_id=target_message_id,
                                             target_post_link=link)
                        session.add(post)
                        await session.flush()
                        pair.last_seen_message_id = max(pair.last_seen_message_id, message_id)
                        pair.last_run_at = datetime.now(timezone.utc)
                        pair.last_success_at = datetime.now(timezone.utc)
                        pair.last_error = None
                        progress = await session.scalar(select(DailyPairProgress).where(
                            DailyPairProgress.pair_id == pair_id, DailyPairProgress.run_date == today))
                        if progress is None:
                            raise RuntimeError("Daily progress row disappeared during forwarding")
                        progress.forwarded_count = (progress.forwarded_count or 0) + 1
                        progress.last_forwarded_at = datetime.now(timezone.utc)
                        progress.updated_at = datetime.now(timezone.utc)
                    new_ids.append((message_id, target_message_id))
                    forwarded_new += 1
                except asyncio.CancelledError: raise
                except Exception as exc:
                    async with self.db.session() as session:
                        pair = await session.get(Pair, pair_id)
                        if pair: pair.last_error = f"{type(exc).__name__}: {exc}"[:1000]
                        progress = await session.scalar(select(DailyPairProgress).where(
                            DailyPairProgress.pair_id == pair_id, DailyPairProgress.run_date == today))
                        if progress: progress.last_error = f"{type(exc).__name__}: {exc}"[:1000]
                    logger.warning("Forward failed for pair %s message %s: %s", pair_id, message_id, exc)
                    break  # leave the failing source message at the checkpoint for retry

        # Pin the latest N messages from this batch when pin mode is enabled.
        if p["pin_mode"] != "none" and p["pin_count"] > 0 and new_ids:
            for _, message_id in new_ids[-max(1, min(int(p["pin_count"]), 10)):]:
                try: await self.telegram.pin_message(target_chat_id, message_id)
                except Exception as exc:
                    logger.warning("Pin failed for pair %s, target message %s: %s", pair_id, message_id, exc)
                    await self.log_db("pin_failed", f"pair={pair_id}, target_message={message_id}: {exc}", "WARNING")

        async with self.db.session() as session:
            pair = await session.get(Pair, pair_id)
            progress = await session.scalar(select(DailyPairProgress).where(
                DailyPairProgress.pair_id == pair_id, DailyPairProgress.run_date == today))
            if not pair or not progress or progress.completed_at: return
            count = int(progress.forwarded_count or 0)
            quota_met = count >= max(1, pair.post_count)
            if not quota_met and cutoff_reached and count == 0:
                # No posts by cutoff closes the day but does not advance the Day number.
                progress.completed_at = datetime.now(timezone.utc)
                pair.last_run_at = datetime.now(timezone.utc)
                return
            if not quota_met and not (cutoff_reached and count > 0):
                return
            links = list((await session.scalars(select(ForwardedPost.target_post_link).where(
                ForwardedPost.pair_id == pair_id, ForwardedPost.run_date == today).order_by(ForwardedPost.id))).all())
            day_no = progress.day_number
            report_username = pair.report_username or ""
            channel_link = channel_link_for_chat(pair.target_chat_id, pair.target_username)
            if report_username.strip():
                existing_outbox = await session.scalar(select(PairReportOutbox.id).where(
                    PairReportOutbox.pair_id == pair_id, PairReportOutbox.report_date == today))
                if existing_outbox is None:
                    session.add(PairReportOutbox(
                        pair_id=pair_id, report_date=today, day_number=day_no,
                        report_username=report_username, channel_link=channel_link,
                        post_links="\n".join(links), body=build_pair_report(day_no, channel_link, links),
                        status="pending"))
            # Forwarding quota completion is independent of Report User delivery. A failed DM
            # remains in the outbox and must not block Smart Reporting or the next Day cycle.
            at = datetime.now(timezone.utc)
            progress.reported_at = None
            progress.completed_at = at
            pair.current_day = day_no + 1 if count > 0 else day_no
            pair.last_run_at = at; pair.last_success_at = at; pair.last_error = None

    async def log_db(self, event: str, detail: str, level: str = "INFO"):
        try:
            async with self.db.session() as session:
                session.add(AppLog(event=event[:100], detail=detail[:500], level=level))
        except Exception: logger.exception("Could not store application event")

def build_pair_report(day_number: int, channel_link: str, links: list[str]) -> str:
    if len(links) <= 1:
        return f"Day{day_number}\n{channel_link}\n" + (links[0] if links else "")
    return f"Day{day_number}\n{channel_link}\n\n" + "\n".join(f"{i}. {link}" for i, link in enumerate(links, 1))
