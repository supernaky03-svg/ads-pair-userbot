from __future__ import annotations
import os
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

def _normalize_database_url(url: str) -> str:
    url = url.strip().strip('"').strip("'")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    blocked = {"sslmode", "ssl", "sslcert", "sslkey", "sslrootcert", "channel_binding"}
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in blocked]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

def _ids(raw: str) -> set[int]:
    result: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if item:
            try: result.add(int(item))
            except ValueError as exc: raise RuntimeError(f"Invalid Telegram user ID: {item!r}") from exc
    return result

def _valid_time(name: str, value: str) -> str:
    try:
        if len(value) != 5 or value[2] != ":": raise ValueError
        hour, minute = map(int, value.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59): raise ValueError
    except Exception as exc:
        raise RuntimeError(f"{name} must be a valid HH:MM time, for example 09:00") from exc
    return value

@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    session_string: str
    bot_token: str
    app_secret: str
    public_base_url: str
    database_url: str
    database_ssl: bool
    admin_user_ids: set[int]
    legacy_owner_telegram_id: int | None
    timezone: str
    daily_run_time: str
    cutoff_time: str
    retry_interval_minutes: int
    scan_limit_per_pair: int
    job_delay_seconds: float
    scheduler_poll_seconds: int
    report_check_interval_seconds: int
    cleanup_interval_seconds: int
    host: str
    port: int
    health_path: str
    @property
    def tzinfo(self) -> ZoneInfo: return ZoneInfo(self.timezone)
    @property
    def telegram_worker_enabled(self) -> bool:
        return bool(self.api_id and self.api_hash and self.session_string)

def load_settings() -> Settings:
    admins = _ids(os.getenv("ADMIN_USER_IDS", ""))
    legacy = os.getenv("LEGACY_OWNER_TELEGRAM_ID", "").strip()
    legacy_id = int(legacy) if legacy else (min(admins) if admins else None)
    timezone = os.getenv("TIMEZONE", "Asia/Yangon").strip() or "Asia/Yangon"
    try: ZoneInfo(timezone)
    except Exception as exc: raise RuntimeError(f"Unknown TIMEZONE: {timezone}") from exc
    return Settings(
        api_id=int(os.getenv("API_ID", "0") or 0), api_hash=os.getenv("API_HASH", "").strip(),
        session_string=os.getenv("TELETHON_SESSION_STRING", "").strip(), bot_token=os.getenv("BOT_TOKEN", "").strip(),
        app_secret=os.getenv("APP_SECRET", "dev-only-change-this-secret"),
        public_base_url=os.getenv("PUBLIC_BASE_URL", "http://localhost:10000").rstrip("/"),
        database_url=_normalize_database_url(os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./ads_pair_dev.db")),
        database_ssl=os.getenv("DATABASE_SSL", "true").strip().lower() in {"1", "true", "yes", "on"},
        admin_user_ids=admins, legacy_owner_telegram_id=legacy_id,
        timezone=timezone, daily_run_time=_valid_time("DAILY_RUN_TIME", os.getenv("DAILY_RUN_TIME", "09:00").strip()),
        cutoff_time=_valid_time("CUTOFF_TIME", os.getenv("CUTOFF_TIME", "23:00").strip()),
        retry_interval_minutes=max(5, int(os.getenv("RETRY_INTERVAL_MINUTES", "60"))),
        scan_limit_per_pair=max(1, min(1000, int(os.getenv("SCAN_LIMIT_PER_PAIR", "100")))),
        job_delay_seconds=max(0.0, float(os.getenv("JOB_DELAY_SECONDS", "5"))),
        scheduler_poll_seconds=max(60, int(os.getenv("SCHEDULER_POLL_SECONDS", "120"))),
        report_check_interval_seconds=max(120, int(os.getenv("REPORT_CHECK_INTERVAL_SECONDS", "300"))),
        cleanup_interval_seconds=max(3600, int(os.getenv("CLEANUP_INTERVAL_SECONDS", "86400"))),
        host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "10000")),
        health_path=os.getenv("HEALTH_PATH", "/healthz"),
    )

settings = load_settings()
