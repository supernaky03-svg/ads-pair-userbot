from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession


async def main() -> None:
    load_dotenv()
    api_id = int(os.getenv("API_ID") or input("API_ID: ").strip())
    api_hash = os.getenv("API_HASH") or input("API_HASH: ").strip()

    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        print("\nYour string session is below. Keep it private.\n")
        print(client.session.save())


if __name__ == "__main__":
    asyncio.run(main())
