from __future__ import annotations

import re


def normalize_username(value: str) -> str:
    value = value.strip()
    if value.startswith("https://t.me/"):
        value = value.rsplit("/", 1)[-1]
    if value.startswith("t.me/"):
        value = value.rsplit("/", 1)[-1]
    if not value.startswith("@"):
        value = "@" + value
    return value


def is_valid_time_hhmm(value: str) -> bool:
    return bool(re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value.strip()))
