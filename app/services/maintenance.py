from __future__ import annotations
from datetime import datetime, timedelta, timezone
from sqlalchemy import delete, select
from app.config import Settings
from app.db.models import AppLog, ForwardedPost, ReportLog, PairReportOutbox, SmartReport, DailyPairProgress, AccessKey
from app.db.session import Database

def utc_cutoff(days: int = 7): return datetime.now(timezone.utc) - timedelta(days=days)

async def cleanup_old_records(db: Database, settings: Settings) -> dict[str, int]:
    """One daily batched cleanup; keep permanent accounts, pair settings, and source checkpoints."""
    cutoff = utc_cutoff(7)
    local_cutoff_date = datetime.now(settings.tzinfo).date() - timedelta(days=6)
    counts: dict[str, int] = {}
    async with db.session() as session:
        # Delete history/report/log detail after exactly the 7-day retention window.
        for key, model, column in [
            ("app_logs", AppLog, AppLog.created_at),
            ("forwarded_posts", ForwardedPost, ForwardedPost.forwarded_at),
            ("report_logs", ReportLog, ReportLog.sent_at),
        ]:
            result = await session.execute(delete(model).where(column < cutoff))
            counts[key] = max(0, int(result.rowcount or 0))
        result = await session.execute(delete(PairReportOutbox).where(PairReportOutbox.created_at < cutoff))
        counts["pair_report_outbox"] = max(0, int(result.rowcount or 0))
        result = await session.execute(delete(SmartReport).where(SmartReport.created_at < cutoff))
        counts["smart_reports"] = max(0, int(result.rowcount or 0))
        # Revoked key hashes are retained for the same 7-day troubleshooting window only.
        result = await session.execute(delete(AccessKey).where(
            AccessKey.active.is_(False), AccessKey.revoked_at.is_not(None), AccessKey.revoked_at < cutoff))
        counts["revoked_keys"] = max(0, int(result.rowcount or 0))
        # Daily progress rows older than seven calendar dates are history, not resumable current work.
        # A current-day row and Pair.last_seen_message_id remain available for restart recovery.
        result = await session.execute(delete(DailyPairProgress).where(
            DailyPairProgress.run_date < local_cutoff_date))
        counts["old_completed_progress"] = max(0, int(result.rowcount or 0))
    return counts
