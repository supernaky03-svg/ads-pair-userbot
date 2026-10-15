from __future__ import annotations
import asyncio, logging
from datetime import datetime, timezone
import aiohttp
from sqlalchemy import select
from app.config import Settings
from app.db.session import Database
from app.db.models import AppUser, AccessKey, Pair
from app.utils import key_hash, new_access_key
logger = logging.getLogger(__name__)

class BotAPI:
    def __init__(self, settings: Settings, db: Database):
        self.settings, self.db = settings, db
        self.base = f"https://api.telegram.org/bot{settings.bot_token}"
        self._offset = 0
        self._http: aiohttp.ClientSession | None = None

    async def _call(self, method: str, payload: dict | None = None, timeout: int = 65):
        if not self.settings.bot_token: return None
        if self._http is None or self._http.closed:
            # A per-session 15s timeout would break Telegram long polling (50s); set timeout per request.
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=70))
        async with self._http.post(
            f"{self.base}/{method}", json=payload or {}, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as response:
            data = await response.json(content_type=None)
            if response.status >= 400 or not data.get("ok"):
                raise RuntimeError(f"Telegram Bot API {method} failed: {data.get('description', response.status)}")
            return data.get("result")

    async def send_message(self, chat_id: int, text: str, *, parse_mode: str | None = None, reply_markup: dict | None = None):
        payload = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
        if parse_mode: payload["parse_mode"] = parse_mode
        if reply_markup: payload["reply_markup"] = reply_markup
        return await self._call("sendMessage", payload, timeout=25)

    async def _ensure_user(self, telegram_id: int) -> AppUser:
        async with self.db.session() as session:
            user = await session.scalar(select(AppUser).where(AppUser.telegram_id == telegram_id))
            if user:
                if telegram_id in self.settings.admin_user_ids and not user.is_admin: user.is_admin = True
                return user
            user = AppUser(telegram_id=telegram_id, is_admin=telegram_id in self.settings.admin_user_ids)
            session.add(user); await session.flush(); await session.refresh(user)
            return user

    async def _rotate_key(self, user_id: int) -> str:
        raw = new_access_key()
        async with self.db.session() as session:
            now = datetime.now(timezone.utc)
            old = (await session.scalars(select(AccessKey).where(AccessKey.user_id == user_id, AccessKey.active.is_(True)))).all()
            for key in old: key.active = False; key.revoked_at = now
            session.add(AccessKey(user_id=user_id, key_hash=key_hash(raw), active=True))
        return raw

    async def _revoke(self, user_id: int):
        async with self.db.session() as session:
            now = datetime.now(timezone.utc)
            rows = (await session.scalars(select(AccessKey).where(AccessKey.user_id == user_id, AccessKey.active.is_(True)))).all()
            for row in rows: row.active = False; row.revoked_at = now

    async def _reply_command(self, message: dict):
        chat = message.get("chat", {}); from_user = message.get("from", {})
        text = (message.get("text") or "").strip()
        if not chat or not from_user or not text.startswith("/"): return
        telegram_id = int(from_user["id"]); chat_id = int(chat["id"])
        command = text.split()[0].split("@", 1)[0].lower()
        args = text.split()[1:]
        # Keys must never be emitted to a group where other members can copy them.
        if command in {"/key", "/revoke", "/createuser"} and chat.get("type") != "private":
            await self.send_message(chat_id, "For account security, use this command in a private chat with the bot.")
            return
        user = await self._ensure_user(telegram_id)
        app_url = self.settings.public_base_url + "/app"
        if command in {"/start", "/help"}:
            markup = {"inline_keyboard": [[{"text": "Open Dashboard", "web_app": {"url": app_url}}]]} if self.settings.public_base_url.startswith("https://") else None
            await self.send_message(chat_id,
                "<b>Ads Pair Manager</b>\n\n"
                "Manage your forwarding pairs and reports in the Mini App.\n\n"
                "<code>/key</code> — generate/rotate your private access key\n"
                "<code>/revoke</code> — revoke the current key\n"
                "<code>/status</code> — workspace summary\n\n"
                "Your workspace belongs to your Telegram account; keys do not transfer ownership.",
                parse_mode="HTML", reply_markup=markup)
        elif command == "/key":
            raw = await self._rotate_key(user.id)
            await self.send_message(chat_id,
                "<b>Your new access key (shown once)</b>\n\n<code>" + raw + "</code>\n\n"
                "Paste it into the Mini App. Generating a new key invalidates your old key and sessions.", parse_mode="HTML")
        elif command == "/revoke":
            await self._revoke(user.id)
            await self.send_message(chat_id, "Access key revoked. Account and pairs remain. Use /key to create a new key.")
        elif command == "/status":
            async with self.db.session() as session:
                pairs = (await session.scalars(select(Pair).where(Pair.owner_user_id == user.id))).all()
            active = sum(1 for p in pairs if p.active)
            await self.send_message(chat_id, f"Workspace status\nPairs: {len(pairs)}\nActive: {active}\nAccess: {'Admin' if user.is_admin else 'User'}")
        elif command == "/createuser":
            if telegram_id not in self.settings.admin_user_ids:
                await self.send_message(chat_id, "Admin only."); return
            if not args:
                await self.send_message(chat_id, "Usage: /createuser TELEGRAM_ID"); return
            try: target_id = int(args[0])
            except ValueError:
                await self.send_message(chat_id, "Telegram ID must be a number."); return
            target = await self._ensure_user(target_id)
            async with self.db.session() as session:
                existing_key = await session.scalar(select(AccessKey.id).where(
                    AccessKey.user_id == target.id, AccessKey.active.is_(True)))
            if existing_key:
                await self.send_message(chat_id,
                    "This account already has an active key. The existing key was not changed. "
                    "Ask the owner to use /key or /revoke first.")
                return
            raw = await self._rotate_key(target.id)
            await self.send_message(chat_id,
                f"Account prepared for <code>{target_id}</code>.\nSend the key to the user privately:\n\n<code>{raw}</code>", parse_mode="HTML")
        else:
            await self.send_message(chat_id, "Unknown command. Use /help.")

    async def run(self):
        if not self.settings.bot_token: return
        # This service uses long polling. Remove a lingering webhook to prevent getUpdates conflicts.
        try: await self._call("deleteWebhook", {"drop_pending_updates": False}, timeout=15)
        except Exception as exc: logger.warning("Could not clear Bot API webhook: %s", exc)
        while True:
            try:
                updates = await self._call("getUpdates", {"offset": self._offset, "timeout": 50,
                                                         "allowed_updates": ["message"]}, timeout=65) or []
                for update in updates:
                    self._offset = max(self._offset, int(update["update_id"]) + 1)
                    try: await self._reply_command(update.get("message", {}))
                    except Exception: logger.exception("Bot command failed for update %s", update.get("update_id"))
            except asyncio.CancelledError: raise
            except Exception as exc:
                logger.warning("Bot polling error: %s", exc)
                await asyncio.sleep(5)

    async def close(self):
        if self._http and not self._http.closed: await self._http.close()
