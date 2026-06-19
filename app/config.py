from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _parse_admin_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            ids.add(int(item))
        except ValueError as exc:
            raise RuntimeError(f"Invalid ADMIN_USER_IDS item: {item!r}") from exc
    return ids


def _normalize_database_url(url: str) -> str:
    """Return a SQLAlchemy asyncpg URL that is safe for Neon/Render.

    Neon often gives URLs like:
      postgresql://user:pass@host/db?sslmode=require&channel_binding=require

    SQLAlchemy's asyncpg dialect can forward unsupported/malformed libpq query
    params to asyncpg. To avoid startup errors such as:
      `sslmode` parameter must be one of: ...

    we remove SSL/channel-binding query params from the URL here and force SSL
    through asyncpg connect_args in app/db/session.py instead.
    """
    url = url.strip().strip('"').strip("'")

    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]

    parts = urlsplit(url)
    query_items = []
    blocked_keys = {
        "sslmode",
        "ssl",
        "sslcert",
        "sslkey",
        "sslrootcert",
        "channel_binding",
    }
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in blocked_keys:
            continue
        query_items.append((key, value))

    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query_items), parts.fragment))


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    session_string: str
    database_url: str
    database_ssl: bool

    admin_user_ids: set[int]
    control_mode: str
    control_group_id: int | None

    timezone: str
    daily_run_time: str
    scan_limit_per_pair: int
    job_delay_seconds: int
    retry_interval_minutes: int
    cutoff_time: str

    host: str
    port: int
    health_path: str

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "require", "required"}


def load_settings() -> Settings:
    control_mode = os.getenv("CONTROL_MODE", "group").strip().lower()
    if control_mode not in {"group", "saved"}:
        raise RuntimeError("CONTROL_MODE must be either 'group' or 'saved'")

    group_raw = os.getenv("CONTROL_GROUP_ID", "").strip()
    control_group_id = int(group_raw) if group_raw else None
    if control_mode == "group" and control_group_id is None:
        raise RuntimeError("CONTROL_GROUP_ID is required when CONTROL_MODE=group")

    daily_time = os.getenv("DAILY_RUN_TIME", "09:00").strip()
    cutoff_time = os.getenv("CUTOFF_TIME", "23:00").strip() or "23:00"

    def validate_hhmm(name: str, value: str) -> None:
        if len(value) != 5 or value[2] != ":":
            raise RuntimeError(f"{name} must be HH:MM, example 09:00")
        hour, minute = value.split(":", 1)
        if not (hour.isdigit() and minute.isdigit() and 0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
            raise RuntimeError(f"{name} must be valid HH:MM, example 09:00")

    validate_hhmm("DAILY_RUN_TIME", daily_time)
    validate_hhmm("CUTOFF_TIME", cutoff_time)

    return Settings(
        api_id=int(_required("API_ID")),
        api_hash=_required("API_HASH"),
        session_string=_required("TELETHON_SESSION_STRING"),
        database_url=_normalize_database_url(_required("DATABASE_URL")),
        # Neon requires SSL. Keep this true on Render. Set DATABASE_SSL=false only for local Postgres without SSL.
        database_ssl=_env_bool("DATABASE_SSL", True),
        admin_user_ids=_parse_admin_ids(os.getenv("ADMIN_USER_IDS", "")),
        control_mode=control_mode,
        control_group_id=control_group_id,
        timezone=os.getenv("TIMEZONE", "Asia/Yangon").strip() or "Asia/Yangon",
        daily_run_time=daily_time,
        scan_limit_per_pair=int(os.getenv("SCAN_LIMIT_PER_PAIR", "100")),
        job_delay_seconds=int(os.getenv("JOB_DELAY_SECONDS", "5")),
        retry_interval_minutes=int(os.getenv("RETRY_INTERVAL_MINUTES", "60")),
        cutoff_time=cutoff_time,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "10000")),
        health_path=os.getenv("HEALTH_PATH", "/healthz"),
    )
