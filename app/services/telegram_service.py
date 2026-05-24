from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from telethon import TelegramClient
from telethon.tl.custom.message import Message
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.types import Channel, ChannelParticipantAdmin, ChannelParticipantCreator

from app.services.link_builder import build_post_link, entity_title, entity_username, normalize_channel_link


@dataclass(slots=True)
class ResolvedChat:
    input_value: str
    chat_id: int
    title: str | None
    username: str | None
    link: str


@dataclass(slots=True)
class ForwardResult:
    source_message_id: int
    target_message_id: int
    target_post_link: str


class TelegramService:
    def __init__(self, client: TelegramClient) -> None:
        self.client = client

    async def resolve_chat(self, input_value: str) -> ResolvedChat:
        entity = await self.client.get_entity(input_value)
        chat_id = int(entity.id)
        # Channels use -100... IDs in message.peer_id/channel contexts. Telethon entity.id is bare ID.
        if isinstance(entity, Channel):
            chat_id = int(f"-100{entity.id}")
        return ResolvedChat(
            input_value=input_value,
            chat_id=chat_id,
            title=entity_title(entity),
            username=entity_username(entity),
            link=normalize_channel_link(input_value, entity),
        )

    async def latest_message_id(self, chat_input: str | int) -> int:
        messages = await self.client.get_messages(chat_input, limit=1)
        if not messages:
            return 0
        return int(messages[0].id)

    async def iter_new_messages(self, chat_input: str | int, *, after_id: int, limit: int) -> list[Message]:
        messages: list[Message] = []
        async for message in self.client.iter_messages(chat_input, min_id=after_id, reverse=True, limit=limit):
            if message.id and not message.action:
                messages.append(message)
        return messages

    async def forward_messages(
        self,
        *,
        source_chat: str | int,
        target_chat: str | int,
        source_messages: Iterable[Message],
        target_chat_id: int,
        target_username: str | None,
        target_input: str,
    ) -> list[ForwardResult]:
        ids = [message.id for message in source_messages if message.id]
        if not ids:
            return []

        forwarded = await self.client.forward_messages(target_chat, ids, from_peer=source_chat)
        if not isinstance(forwarded, list):
            forwarded = [forwarded]

        results: list[ForwardResult] = []
        for source_id, target_msg in zip(ids, forwarded):
            target_id = int(target_msg.id)
            results.append(
                ForwardResult(
                    source_message_id=int(source_id),
                    target_message_id=target_id,
                    target_post_link=build_post_link(
                        target_chat_id=target_chat_id,
                        target_username=target_username,
                        target_input=target_input,
                        message_id=target_id,
                    ),
                )
            )
        return results

    async def pin_message(self, target_chat: str | int, message_id: int) -> None:
        await self.client.pin_message(target_chat, message_id, notify=False)

    async def send_report(self, username: str, text: str) -> None:
        await self.client.send_message(username, text)

    async def assert_target_admin(self, target_chat: str | int) -> None:
        """Best-effort target admin check.

        Telegram/Telethon does not always expose channel admin rights in the
        same way for broadcast channels vs supergroups. In some valid cases
        an admin can post and pin, but ``admin_rights.post_messages`` or
        ``admin_rights.pin_messages`` is returned as ``None``/``False``.

        So this method only blocks the clearly-wrong case: the session account
        is neither creator nor admin. The real source of truth remains the
        actual forward/pin operation, which will raise a Telegram error if the
        permission is really missing.
        """
        me = await self.client.get_me()
        entity = await self.client.get_entity(target_chat)
        try:
            participant = await self.client(GetParticipantRequest(entity, me.id))
        except Exception:
            # Some chats do not support GetParticipantRequest. Do not block
            # pair creation; actual send/pin will validate permissions later.
            return

        p = participant.participant
        if isinstance(p, (ChannelParticipantCreator, ChannelParticipantAdmin)):
            return

        raise RuntimeError(
            "Session account is not an admin in the target channel. "
            "Add it as admin with Post Messages and Pin Messages permissions."
        )
