from __future__ import annotations
import asyncio, logging, os
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timezone
from typing import AsyncIterator
from fastapi import FastAPI, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.db.session import Database
from app.db.models import AppUser, AccessKey, Pair, DailyPairProgress, SmartReport, ForwardedPost, AppLog
from app.utils import key_hash, new_access_key, validate_telegram_init_data, create_session_token, read_session_token, valid_hhmm, initial_next_reset_date
from app.services.telegram_gateway import TelegramGateway
from app.services.bot_api import BotAPI
from app.services.forwarding import ForwardingEngine
from app.services.reporting import SmartReporting
from app.services.maintenance import cleanup_old_records

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)
WEB_DIR = Path(__file__).resolve().parents[2] / "web"

async def maintenance_loop(db, settings):
    while True:
        try:
            result = await cleanup_old_records(db, settings)
            logger.info("7-day cleanup finished: %s", result)
        except asyncio.CancelledError: raise
        except Exception: logger.exception("Cleanup job failed")
        await asyncio.sleep(max(3600, settings.cleanup_interval_seconds))

async def scheduler_loop(engine, reporter, settings, wake_event: asyncio.Event):
    while True:
        # Do not hit Neon every few seconds while the Telethon session is offline. Try a bounded
        # reconnect, keep Smart Reporting available, then sleep until a change or slow retry.
        if not engine.telegram.connected:
            try:
                await engine.telegram.ensure_connected()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Telethon reconnect attempt failed")
            if not engine.telegram.connected:
                if reporter is not None:
                    try: await reporter.tick()
                    except asyncio.CancelledError: raise
                    except Exception: logger.exception("Smart Reporting check while sender is offline failed")
                wake_event.clear()
                try:
                    await asyncio.wait_for(wake_event.wait(), timeout=max(300, settings.scheduler_poll_seconds))
                except asyncio.TimeoutError:
                    pass
                continue
        # Clear before work, so changes made during a tick remain signalled.
        wake_event.clear()
        try:
            await engine.tick()
            # Smart Reporting must run before the optional per-pair Report User delivery queue.
            await reporter.tick()
            await engine.retry_pending_pair_reports()
            delay = await engine.next_wakeup_seconds()
        except asyncio.CancelledError: raise
        except Exception:
            logger.exception("Forwarding scheduler tick failed")
            delay = settings.scheduler_poll_seconds
        if wake_event.is_set():
            continue
        try:
            await asyncio.wait_for(wake_event.wait(), timeout=max(1, delay))
        except asyncio.TimeoutError:
            pass

async def report_loop(reporter, settings):
    while True:
        try: await reporter.tick()
        except asyncio.CancelledError: raise
        except Exception: logger.exception("Smart Reporting tick failed")
        await asyncio.sleep(settings.report_check_interval_seconds)

@asynccontextmanager
async def lifespan(app: FastAPI):
    db = Database(settings)
    await db.init()
    telegram = TelegramGateway(settings)
    bot = BotAPI(settings, db) if settings.bot_token else None
    try:
        await telegram.start()
    except Exception: logger.exception("Telethon startup failed; app/health endpoints remain available")
    engine = ForwardingEngine(db, telegram, bot, settings)
    fallback_id = settings.legacy_owner_telegram_id or telegram.account_id
    await engine.assign_legacy_pairs(fallback_id)
    reporter = SmartReporting(db, bot, settings)
    app.state.db, app.state.telegram, app.state.bot = db, telegram, bot
    app.state.engine, app.state.reporter = engine, reporter
    tasks = [asyncio.create_task(maintenance_loop(db, settings))]
    scheduler_wakeup = asyncio.Event()
    app.state.scheduler_wakeup = scheduler_wakeup
    if telegram.connected: tasks += [asyncio.create_task(scheduler_loop(engine, reporter, settings, scheduler_wakeup))]
    if bot: tasks.append(asyncio.create_task(bot.run()))
    if bot and not telegram.connected: tasks.append(asyncio.create_task(report_loop(reporter, settings)))
    app.state.tasks = tasks
    if (not settings.app_secret or len(settings.app_secret) < 32 or
            settings.app_secret == "dev-only-change-this-secret" or "replace" in settings.app_secret.lower()):
        logger.warning("APP_SECRET is missing, a placeholder, or too short. Mini App login will be disabled until fixed.")
    try: yield
    finally:
        for task in tasks: task.cancel()
        for task in tasks:
            try: await task
            except asyncio.CancelledError: pass
        await telegram.stop()
        if bot: await bot.close()
        await db.close()

app = FastAPI(title="Ads Pair Manager", version="2.0.0", lifespan=lifespan)

async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    db: Database = request.app.state.db
    async with db.sessionmaker() as session:
        yield session

class AuthInput(BaseModel):
    init_data: str = Field(min_length=1, max_length=10000)
    access_key: str = Field(min_length=20, max_length=200)

class AdminUserInput(BaseModel):
    telegram_id: int = Field(ge=1)

class PairInput(BaseModel):
    name: str = Field(default="Untitled Pair", max_length=120)
    source_channel: str = Field(min_length=1, max_length=255)
    target_channel: str = Field(min_length=1, max_length=255)
    report_user: str | None = Field(default=None, max_length=255)
    post_count: int = Field(default=1, ge=1, le=50)
    first_day: int = Field(default=1, ge=1, le=31)
    active: bool = True
    pin_enabled: bool = True
    pin_count: int = Field(default=1, ge=1, le=10)
    start_time: str | None = None
    cutoff_time: str | None = None
    @field_validator("start_time", "cutoff_time")
    @classmethod
    def validate_time(cls, value):
        if value is not None and not valid_hhmm(value): raise ValueError("Time must be HH:MM")
        return value
    @field_validator("report_user")
    @classmethod
    def normalize_report_user(cls, value):
        if value is None or not value.strip(): return None
        clean = value.strip()
        if clean.startswith("@") or clean.lstrip("-").isdigit(): return clean
        return "@" + clean
    @model_validator(mode="after")
    def validate_time_window(self):
        # Compare effective defaults too; overnight windows aren't supported by the daily cycle.
        start = self.start_time or settings.daily_run_time
        cutoff = self.cutoff_time or settings.cutoff_time
        if start >= cutoff:
            raise ValueError("Start time must be earlier than cutoff time; overnight windows are not supported")
        return self

async def authed_user(request: Request, authorization: str | None = Header(default=None), db: AsyncSession = Depends(get_db)) -> AppUser:
    if (not settings.app_secret or len(settings.app_secret) < 32 or
            settings.app_secret == "dev-only-change-this-secret" or "replace" in settings.app_secret.lower()):
        raise HTTPException(503, "APP_SECRET is not configured. Set a random secret of at least 32 characters.")
    if not authorization or not authorization.startswith("Bearer "): raise HTTPException(401, "Enter your access key to unlock the workspace")
    try: payload = read_session_token(authorization[7:], settings.app_secret)
    except ValueError: raise HTTPException(401, "Session expired. Sign in again.")
    key = await db.scalar(select(AccessKey).where(AccessKey.id == int(payload["kid"]), AccessKey.user_id == int(payload["uid"]), AccessKey.active.is_(True)))
    user = await db.get(AppUser, int(payload["uid"]))
    if not key or not user: raise HTTPException(401, "Access key was revoked. Sign in again.")
    return user

def serialize_pair(pair: Pair, progress: DailyPairProgress | None = None):
    return {"id": pair.id, "name": pair.pair_name or pair.target_title or pair.target_username or pair.target_input,
            "source_channel": pair.source_input, "target_channel": pair.target_input,
            "report_user": pair.report_username or "", "post_count": pair.post_count,
            "first_day": pair.first_day, "current_day": pair.current_day, "active": pair.active,
            "pin_enabled": pair.pin_mode != "none", "pin_count": pair.pin_count,
            "start_time": pair.start_time or settings.daily_run_time, "cutoff_time": pair.cutoff_time or settings.cutoff_time,
            "custom_start_time": pair.start_time, "custom_cutoff_time": pair.cutoff_time,
            "last_seen_message_id": pair.last_seen_message_id, "last_error": pair.last_error,
            "today_forwarded": progress.forwarded_count if progress else 0,
            "today_completed": bool(progress and progress.completed_at)}

@app.get("/")
def root(): return {"app": "/app", "health": settings.health_path, "service": "Ads Pair Manager"}

@app.get("/app")
def mini_app(): return FileResponse(WEB_DIR / "index.html")

@app.get("/healthz")
def healthz(request: Request):
    # Cheap health response intentionally does not query Neon on every UptimeRobot ping.
    telegram = getattr(request.app.state, "telegram", None)
    worker_connected = bool(telegram and telegram.connected)
    return {"status": "ok", "worker_configured": settings.telegram_worker_enabled,
            "worker_connected": worker_connected, "bot_configured": bool(settings.bot_token)}

@app.post("/api/auth/key")
async def auth_key(payload: AuthInput, db: AsyncSession = Depends(get_db)):
    if not settings.bot_token: raise HTTPException(503, "BotFather bot is not configured")
    if (not settings.app_secret or len(settings.app_secret) < 32 or
            settings.app_secret == "dev-only-change-this-secret" or
            "replace" in settings.app_secret.lower()):
        raise HTTPException(503, "APP_SECRET is not configured. Set a random secret of at least 32 characters.")
    try: identity = validate_telegram_init_data(payload.init_data, settings.bot_token)
    except Exception as exc: raise HTTPException(401, str(exc))
    user = await db.scalar(select(AppUser).where(AppUser.telegram_id == identity["telegram_id"]))
    if not user: raise HTTPException(403, "Account not found. Open the Telegram bot and send /start first.")
    key = await db.scalar(select(AccessKey).where(AccessKey.user_id == user.id, AccessKey.key_hash == key_hash(payload.access_key), AccessKey.active.is_(True)))
    if not key: raise HTTPException(401, "Access key is incorrect, expired or revoked")
    return {"token": create_session_token(user.id, key.id, settings.app_secret), "first_name": identity["first_name"], "is_admin": user.is_admin}

@app.get("/api/me")
async def me(user: AppUser = Depends(authed_user)):
    return {"telegram_id": user.telegram_id, "is_admin": user.is_admin}

@app.get("/api/summary")
async def summary(user: AppUser = Depends(authed_user), db: AsyncSession = Depends(get_db)):
    today = datetime.now(settings.tzinfo).date()
    pairs = (await db.scalars(select(Pair).where(Pair.owner_user_id == user.id))).all()
    pair_ids = [p.id for p in pairs]
    progresses = (await db.scalars(select(DailyPairProgress).where(DailyPairProgress.pair_id.in_(pair_ids), DailyPairProgress.run_date == today))).all() if pair_ids else []
    progress_map = {p.pair_id: p for p in progresses}
    return {"date": today.isoformat(), "pairs": len(pairs), "active": sum(1 for p in pairs if p.active),
            "today_success": sum(int(progress_map[p.id].forwarded_count or 0) for p in pairs if p.id in progress_map),
            "today_quota": sum(max(1, p.post_count) for p in pairs if p.active),
            "completed": sum(1 for p in pairs if progress_map.get(p.id) and progress_map[p.id].completed_at)}

@app.get("/api/pairs")
async def list_pairs(user: AppUser = Depends(authed_user), db: AsyncSession = Depends(get_db)):
    today = datetime.now(settings.tzinfo).date()
    pairs = (await db.scalars(select(Pair).where(Pair.owner_user_id == user.id).order_by(Pair.id.desc()))).all()
    progresses = (await db.scalars(select(DailyPairProgress).where(DailyPairProgress.pair_id.in_([p.id for p in pairs]), DailyPairProgress.run_date == today))).all() if pairs else []
    progress_map = {p.pair_id: p for p in progresses}
    return [serialize_pair(p, progress_map.get(p.id)) for p in pairs]

async def resolve_pair_fields(request: Request, payload: PairInput, db: AsyncSession, user: AppUser, existing: Pair | None = None):
    gateway: TelegramGateway = request.app.state.telegram
    if not gateway.connected: raise HTTPException(503, "Telegram forwarding session is offline; check Render environment variables")
    try:
        src, tgt, latest = await request.app.state.engine.validate_channels(payload.source_channel.strip(), payload.target_channel.strip())
    except Exception as exc: raise HTTPException(400, f"Channel check failed: {exc}")
    if existing and existing.source_chat_id == src.chat_id:
        checkpoint = existing.last_seen_message_id
    else:
        checkpoint = latest  # only forward new posts created after pair setup/source change
    if existing:
        existing.owner_user_id = user.id; existing.pair_name = payload.name.strip(); existing.source_input = payload.source_channel.strip()
        existing.source_chat_id = src.chat_id; existing.source_title = src.title
        # Preserve the user's editable value (not a private /c/ display link) so Edit Pair
        # can resolve the same channel on the next save. Canonical links are built for reports.
        existing.target_input = payload.target_channel.strip(); existing.target_chat_id = tgt.chat_id
        existing.target_title = tgt.title; existing.target_username = tgt.username
        existing.report_username = payload.report_user; existing.post_count = payload.post_count
        if existing.first_day != payload.first_day:
            # Make the Start Day field operational. Changing it intentionally restarts this pair's Day cycle.
            today = datetime.now(settings.tzinfo).date()
            existing.current_day = payload.first_day
            existing.next_reset_date = initial_next_reset_date(today, payload.first_day)
        existing.first_day = payload.first_day; existing.active = payload.active
        existing.pin_mode = "all" if payload.pin_enabled else "none"; existing.pin_count = payload.pin_count
        existing.start_time = payload.start_time; existing.cutoff_time = payload.cutoff_time
        existing.last_seen_message_id = checkpoint
        return existing
    from app.utils import initial_next_reset_date
    today = datetime.now(settings.tzinfo).date()
    pair = Pair(owner_user_id=user.id, pair_name=payload.name.strip(), report_username=payload.report_user,
                source_input=payload.source_channel.strip(), source_chat_id=src.chat_id, source_title=src.title,
                target_input=payload.target_channel.strip(), target_chat_id=tgt.chat_id, target_title=tgt.title, target_username=tgt.username,
                first_day=payload.first_day, current_day=payload.first_day, next_reset_date=initial_next_reset_date(today, payload.first_day),
                post_count=payload.post_count, last_seen_message_id=checkpoint, active=payload.active,
                pin_mode="all" if payload.pin_enabled else "none", pin_count=payload.pin_count,
                start_time=payload.start_time, cutoff_time=payload.cutoff_time)
    db.add(pair)
    return pair

@app.post("/api/pairs")
async def create_pair(payload: PairInput, request: Request, user: AppUser = Depends(authed_user), db: AsyncSession = Depends(get_db)):
    pair = await resolve_pair_fields(request, payload, db, user)
    await db.commit(); await db.refresh(pair)
    wake = getattr(request.app.state, "scheduler_wakeup", None)
    if wake: wake.set()
    return {"message": "Pair created. Only new source posts will be forwarded.", "id": pair.id}

@app.put("/api/pairs/{pair_id}")
async def update_pair(pair_id: int, payload: PairInput, request: Request, user: AppUser = Depends(authed_user), db: AsyncSession = Depends(get_db)):
    pair = await db.scalar(select(Pair).where(Pair.id == pair_id, Pair.owner_user_id == user.id))
    if not pair: raise HTTPException(404, "Pair not found")
    await resolve_pair_fields(request, payload, db, user, pair)
    await db.commit()
    wake = getattr(request.app.state, "scheduler_wakeup", None)
    if wake: wake.set()
    return {"message": "Pair updated"}

@app.delete("/api/pairs/{pair_id}")
async def delete_pair(pair_id: int, request: Request, user: AppUser = Depends(authed_user), db: AsyncSession = Depends(get_db)):
    pair = await db.scalar(select(Pair).where(Pair.id == pair_id, Pair.owner_user_id == user.id))
    if not pair: raise HTTPException(404, "Pair not found")
    await db.delete(pair); await db.commit()
    wake = getattr(request.app.state, "scheduler_wakeup", None)
    if wake: wake.set()
    return {"message": "Pair deleted; this cannot be undone."}

@app.get("/api/reports")
async def get_reports(user: AppUser = Depends(authed_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.scalars(select(SmartReport).where(SmartReport.owner_user_id == user.id, SmartReport.status == "sent").order_by(SmartReport.report_date.desc()).limit(7))).all()
    return [{"date": x.report_date.isoformat(), "status": x.status, "body": x.body, "sent_at": x.sent_at.isoformat() if x.sent_at else None} for x in rows]

@app.post("/api/key/rotate")
async def rotate_key(user: AppUser = Depends(authed_user), db: AsyncSession = Depends(get_db)):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    old = (await db.scalars(select(AccessKey).where(AccessKey.user_id == user.id, AccessKey.active.is_(True)))).all()
    for item in old: item.active = False; item.revoked_at = now
    raw = new_access_key(); item = AccessKey(user_id=user.id, key_hash=key_hash(raw), active=True)
    db.add(item); await db.commit(); await db.refresh(item)
    return {"access_key": raw, "message": "Copy it now. The previous key and its sessions are invalidated."}

@app.post("/api/key/revoke")
async def revoke_key(user: AppUser = Depends(authed_user), db: AsyncSession = Depends(get_db)):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    old = (await db.scalars(select(AccessKey).where(AccessKey.user_id == user.id, AccessKey.active.is_(True)))).all()
    for item in old: item.active = False; item.revoked_at = now
    await db.commit()
    return {"message": "Key revoked. Your permanent account and pair data remain."}

@app.get("/api/admin/users")
async def admin_users(user: AppUser = Depends(authed_user), db: AsyncSession = Depends(get_db)):
    if not user.is_admin: raise HTTPException(403, "Admin only")
    rows = (await db.scalars(select(AppUser).order_by(AppUser.created_at.desc()).limit(100))).all()
    counts = dict((row_id, int(total)) for row_id, total in (await db.execute(
        select(Pair.owner_user_id, func.count(Pair.id)).where(Pair.owner_user_id.in_([u.id for u in rows]))
        .group_by(Pair.owner_user_id)
    )).all()) if rows else {}
    return [{"id": row.id, "telegram_id": row.telegram_id, "is_admin": row.is_admin,
             "pairs": counts.get(row.id, 0)} for row in rows]


@app.post("/api/admin/users")
async def create_admin_user(payload: AdminUserInput, user: AppUser = Depends(authed_user), db: AsyncSession = Depends(get_db)):
    """Admin can create a workspace and issue a one-time key without opening a group-chat secret leak."""
    if not user.is_admin:
        raise HTTPException(403, "Admin only")
    target = await db.scalar(select(AppUser).where(AppUser.telegram_id == payload.telegram_id))
    if target is None:
        target = AppUser(telegram_id=payload.telegram_id, is_admin=payload.telegram_id in settings.admin_user_ids)
        db.add(target)
        await db.flush()
    else:
        active_key = await db.scalar(select(AccessKey.id).where(
            AccessKey.user_id == target.id, AccessKey.active.is_(True)))
        if active_key:
            raise HTTPException(409, "This account already has an active key. Ask the user to use /key, or revoke their current key first.")
    raw = new_access_key()
    db.add(AccessKey(user_id=target.id, key_hash=key_hash(raw), active=True))
    await db.commit()
    return {"telegram_id": target.telegram_id, "is_admin": target.is_admin, "access_key": raw,
            "message": "Key shown once. Send it privately to the owner; the key only works for this Telegram identity."}
