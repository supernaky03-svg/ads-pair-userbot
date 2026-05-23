from __future__ import annotations

from telethon.tl.types import Channel, Chat, User


def entity_title(entity: object) -> str | None:
    return getattr(entity, "title", None) or getattr(entity, "first_name", None)


def entity_username(entity: object) -> str | None:
    username = getattr(entity, "username", None)
    return username if username else None


def normalize_channel_link(input_value: str, entity: object | None = None) -> str:
    username = entity_username(entity) if entity is not None else None
    if username:
        return f"https://t.me/{username}"
    value = input_value.strip()
    if value.startswith("@"):
        return f"https://t.me/{value[1:]}"
    return value


def private_tme_c_id(chat_id: int) -> str:
    raw = str(abs(chat_id))
    if raw.startswith("100"):
        return raw[3:]
    return raw


def build_post_link(*, target_chat_id: int, target_username: str | None, target_input: str, message_id: int) -> str:
    username = target_username
    if not username:
        value = target_input.strip()
        if value.startswith("https://t.me/") and "/+" not in value:
            tail = value.replace("https://t.me/", "", 1).strip("/")
            if tail and "/" not in tail:
                username = tail
        elif value.startswith("@"):
            username = value[1:]
    if username:
        return f"https://t.me/{username}/{message_id}"
    return f"https://t.me/c/{private_tme_c_id(target_chat_id)}/{message_id}"
