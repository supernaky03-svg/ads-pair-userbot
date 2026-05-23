from __future__ import annotations

from telethon import TelegramClient
from telethon.sessions import StringSession

from app.config import Settings


def create_client(settings: Settings) -> TelegramClient:
    return TelegramClient(StringSession(settings.session_string), settings.api_id, settings.api_hash)
