from __future__ import annotations
import asyncio, logging
from dataclasses import dataclass
from typing import Any
from app.config import Settings
logger = logging.getLogger(__name__)

@dataclass(slots=True)
class ChatInfo:
    chat_id: int
    title: str | None
    link: str
    username: str | None

class TelegramGateway:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client: Any = None
        self.account_id: int | None = None

    @property
    def connected(self) -> bool: return bool(self.client and self.client.is_connected())

    async def start(self) -> None:
        if not self.settings.telegram_worker_enabled: return
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        if self.client is None:
            self.client = TelegramClient(StringSession(self.settings.session_string), self.settings.api_id, self.settings.api_hash)
        # Never invoke Telethon's interactive phone-code prompt inside a hosted service.
        if not await self.ensure_connected():
            raise RuntimeError("Telethon session could not connect or is not authorized. Generate an authorized StringSession locally and set it in Render.")

    async def ensure_connected(self) -> bool:
        """Try one non-interactive reconnect; callers decide how long to wait before retrying."""
        if not self.settings.telegram_worker_enabled:
            return False
        if self.client is None:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
            self.client = TelegramClient(StringSession(self.settings.session_string), self.settings.api_id, self.settings.api_hash)
        if self.connected:
            return True
        try:
            await self.client.connect()
            if not await self.client.is_user_authorized():
                await self.client.disconnect()
                logger.error("Telethon session is not authorized; generate a fresh StringSession locally")
                return False
            me = await self.client.get_me()
            self.account_id = int(me.id)
            logger.info("Telethon sender connected as %s (%s)", getattr(me, "username", None) or me.first_name, me.id)
            return self.connected
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Telethon connection/reconnection failed: %s", exc)
            try: await self.client.disconnect()
            except Exception: pass
            return False

    async def stop(self) -> None:
        if self.client: await self.client.disconnect()

    def _required_client(self) -> Any:
        if not self.connected or not self.client: raise RuntimeError("Telethon sender is not connected; check session/API settings")
        return self.client

    async def resolve_chat(self, value: str) -> ChatInfo:
        client = self._required_client()
        entity = await client.get_entity(value)
        from telethon.utils import get_peer_id
        chat_id = int(get_peer_id(entity))
        username = getattr(entity, "username", None)
        if username: link = f"https://t.me/{username}"
        else:
            ident = str(abs(chat_id)); ident = ident[3:] if ident.startswith("100") else ident
            link = f"https://t.me/c/{ident}"
        return ChatInfo(chat_id, getattr(entity, "title", None) or getattr(entity, "first_name", None), link, username)

    async def latest_message_id(self, source: str) -> int:
        client = self._required_client()
        msg = await client.get_messages(source, limit=1)
        return int(msg[0].id) if msg else 0

    async def assert_target_admin(self, target: str) -> None:
        client = self._required_client()
        entity = await client.get_entity(target)
        try:
            permissions = await client.get_permissions(entity, "me")
            if not (getattr(permissions, "is_admin", False) or getattr(permissions, "is_creator", False)):
                raise PermissionError("The sender account must be an admin of the target channel")
            if getattr(permissions, "is_banned", False): raise PermissionError("Sender is banned from the target channel")
        except PermissionError: raise
        except Exception as exc:
            raise PermissionError(f"Could not verify sender permissions in target: {exc}") from exc

    async def send_pair_report(self, username: str, text: str) -> None:
        if not username or not username.strip(): return
        client = self._required_client()
        recipient = username.strip()
        if recipient.lstrip("-").isdigit():
            recipient = int(recipient)
        await client.send_message(recipient, text[:4000], link_preview=False)

    async def forward_one(self, source_chat_id: int, target_chat_id: int, message):
        client = self._required_client()
        result = await client.forward_messages(target_chat_id, message, from_peer=source_chat_id)
        if isinstance(result, list): result = next((x for x in result if x), None)
        if result is None: raise RuntimeError("Telegram did not return the forwarded message")
        return int(result.id)

    async def pin_message(self, target_chat_id: int, message_id: int) -> None:
        client = self._required_client()
        await client.pin_message(target_chat_id, message_id, notify=False)

    async def iter_new_messages(self, source_chat_id: int, after_id: int, limit: int):
        client = self._required_client()
        source = await client.get_entity(source_chat_id)
        messages = []
        async for msg in client.iter_messages(source, min_id=after_id, reverse=True, limit=limit):
            if msg and getattr(msg, "id", None): messages.append(msg)
        return messages
