from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _csv_ints(value: str) -> list[int]:
    out: list[int] = []
    for item in (value or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            out.append(int(item))
        except ValueError:
            continue
    return out


def _first_non_empty_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


@dataclass(slots=True)
class Settings:
    bot_token: str = os.getenv("BOT_TOKEN", "").strip()
    api_id: int = int(os.getenv("API_ID", "0"))
    api_hash: str = os.getenv("API_HASH", "").strip()

    # Support both names:
    # - TELETHON_SESSION_STRING  (recommended)
    # - SESSION_STRING           (backward compatibility)
    session_string: str = _first_non_empty_env(
        "TELETHON_SESSION_STRING",
        "SESSION_STRING",
    )

    database_url: str = os.getenv("DATABASE_URL", "").strip()
    admin_ids: list[int] | None = None

    default_pair_limit: int = int(os.getenv("DEFAULT_PAIR_LIMIT", "20"))
    default_scan_count: int = int(os.getenv("DEFAULT_SCAN_COUNT", "100"))
    poll_interval_seconds: int = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

    # Keep current behavior, but also accept Render PORT if HEALTH_PORT is missing.
    health_port: int = int(
        _first_non_empty_env("HEALTH_PORT", "PORT") or "10000"
    )

    flow_timeout_minutes: int = int(os.getenv("FLOW_TIMEOUT_MINUTES", "30"))
    language_default: str = os.getenv("LANGUAGE_DEFAULT", "en").strip().lower() or "en"
    log_level: str = os.getenv("LOG_LEVEL", "INFO").strip()

    delay_min_seconds: int = int(os.getenv("DELAY_MIN_SECONDS", "0"))
    delay_max_seconds: int = int(os.getenv("DELAY_MAX_SECONDS", "0"))
    recent_ids_limit: int = int(os.getenv("RECENT_IDS_LIMIT", "200"))
    latest_recheck_limit: int = int(os.getenv("LATEST_RECHECK_LIMIT", "10"))

    project_root: Path = Path(__file__).resolve().parents[2]

    def __post_init__(self) -> None:
        self.admin_ids = _csv_ints(os.getenv("ADMIN_IDS", ""))

        if not self.bot_token:
            raise ValueError("BOT_TOKEN is required")

        if not self.api_id or not self.api_hash:
            raise ValueError("API_ID and API_HASH are required")

        if not self.session_string:
            raise ValueError(
                "TELETHON_SESSION_STRING is required "
                "(SESSION_STRING is also accepted for backward compatibility)"
            )

        if not self.database_url:
            raise ValueError("DATABASE_URL is required")


settings = Settings()
