from __future__ import annotations
import asyncio, logging
from datetime import datetime, timezone
from sqlalchemy import select, and_
from sqlalchemy.exc import IntegrityError
from app.config import Settings
from app.db.models import AppUser, Pair, DailyPairProgress, SmartReport, AppLog
from app.db.session import Database
from app.services.forwarding import at_or_after, local_now
from app.utils import report_retry_delay
logger = logging.getLogger(__name__)


def pair_quota_done(pair: Pair, progress: DailyPairProgress | None) -> bool:
    """True only when the required quota was met; cutoff with zero posts is not success."""
    return bool(progress and progress.completed_at and
                int(progress.forwarded_count or 0) >= max(1, int(pair.post_count or 1)))


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class SmartReporting:
    """Mandatory owner reports with durable outbox rows and bounded retry backoff."""
    def __init__(self, db: Database, bot, settings: Settings):
        self.db, self.bot, self.settings = db, bot, settings

    async def tick(self):
        # Without Bot API credentials delivery is impossible; don't burn DB quota creating doomed rows.
        if not self.bot:
            return
        await self._retry_pending_reports()
        today = local_now(self.settings).date()
        now = local_now(self.settings)
        # One joined read for all users/pairs/progress. Avoid a query per pair on each cycle.
        async with self.db.session() as session:
            rows = (await session.execute(
                select(AppUser, Pair, DailyPairProgress)
                .join(Pair, Pair.owner_user_id == AppUser.id)
                .outerjoin(DailyPairProgress, and_(
                    DailyPairProgress.pair_id == Pair.id,
                    DailyPairProgress.run_date == today,
                ))
                .where(Pair.active.is_(True))
                .order_by(AppUser.id, Pair.id)
            )).all()
        grouped: dict[int, tuple[AppUser, list[tuple[Pair, DailyPairProgress | None]]]] = {}
        for user, pair, progress in rows:
            if user.id not in grouped:
                grouped[user.id] = (user, [])
            grouped[user.id][1].append((pair, progress))
        for owner_id, (user, states) in grouped.items():
            try:
                await self._maybe_create_report(user, states, today, now)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Smart Report check failed for owner %s", owner_id)
                async with self.db.session() as session:
                    session.add(AppLog(level="ERROR", event="smart_report_error", detail=f"owner={owner_id}: {exc}"[:500]))
        # Deliver reports created during this tick immediately. A failed send will be retried with backoff.
        await self._retry_pending_reports()

    async def _maybe_create_report(self, owner: AppUser, states: list[tuple[Pair, DailyPairProgress | None]], today, now):
        async with self.db.session() as session:
            existing = await session.scalar(select(SmartReport).where(
                SmartReport.owner_user_id == owner.id, SmartReport.report_date == today))
            if existing is not None:
                # A pending row was already considered by _retry_pending_reports; never create/send duplicates.
                return
            all_done = bool(states) and all(pair_quota_done(pair, progress) for pair, progress in states)
            deadline = at_or_after(now, self.settings.cutoff_time)
            if not all_done and not deadline:
                return
            lines = [f"📊 Smart Report · {today.isoformat()}", ""]
            for pair, progress in states:
                count = int(progress.forwarded_count or 0) if progress else 0
                done = pair_quota_done(pair, progress)
                flag = "✅" if done else "⚠️"
                label = pair.pair_name or pair.target_title or pair.target_username or pair.target_input
                lines.append(f"{flag} {label}: {count}/{max(1, pair.post_count)} posts")
                if progress and progress.last_error:
                    lines.append(f"   Last issue: {progress.last_error[:150]}")
            lines += ["", "Status: " + ("WORK DONE" if all_done else "PARTIAL / INCOMPLETE")]
            report = SmartReport(owner_user_id=owner.id, report_date=today,
                                 status="pending", body="\n".join(lines))
            session.add(report)
            try:
                await session.flush()
            except IntegrityError:
                # Another process may have created the unique user/day report between reads.
                await session.rollback()
                return

    async def _retry_pending_reports(self):
        """Retry all pending dates (not just today's report), respecting persisted exponential backoff."""
        if not self.bot:
            return
        async with self.db.session() as session:
            rows = (await session.execute(
                select(SmartReport.id, SmartReport.owner_user_id, AppUser.telegram_id,
                       SmartReport.last_attempt_at, SmartReport.attempt_count)
                .join(AppUser, AppUser.id == SmartReport.owner_user_id)
                .where(SmartReport.status == "pending")
                .order_by(SmartReport.report_date, SmartReport.id)
                .limit(50)
            )).all()
        now = datetime.now(timezone.utc)
        for report_id, owner_id, telegram_id, last_attempt_at, attempt_count in rows:
            if last_attempt_at is not None:
                retry_after = _as_utc(last_attempt_at).timestamp() + report_retry_delay(
                    self.settings.report_check_interval_seconds, attempt_count)
                if now.timestamp() < retry_after:
                    continue
            body = None
            # Persist the timestamp/count before touching the network. This survives process restarts.
            async with self.db.session() as session:
                report = await session.get(SmartReport, report_id)
                if not report or report.status != "pending":
                    continue
                if report.last_attempt_at is not None:
                    retry_after = _as_utc(report.last_attempt_at).timestamp() + report_retry_delay(
                        self.settings.report_check_interval_seconds, report.attempt_count)
                    if now.timestamp() < retry_after:
                        continue
                report.last_attempt_at = now
                report.attempt_count = int(report.attempt_count or 0) + 1
                body = report.body
            try:
                await self.bot.send_message(telegram_id, body or "")
                async with self.db.session() as session:
                    report = await session.get(SmartReport, report_id)
                    if report and report.status == "pending":
                        report.status = "sent"
                        report.sent_at = datetime.now(timezone.utc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Smart Report send failed for user %s: %s", owner_id, exc)
                async with self.db.session() as session:
                    session.add(AppLog(level="ERROR", event="smart_report_delivery_failed",
                                       detail=f"owner={owner_id}, report={report_id}: {exc}"[:500]))
