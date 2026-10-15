"""Generate a Telethon StringSession locally. Never run this on a shared machine."""
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

async def main():
    api_id = int(input("API_ID: ").strip())
    api_hash = input("API_HASH: ").strip()
    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        print("\nCopy this into TELETHON_SESSION_STRING in Render. Do not share it.\n")
        print(client.session.save())

if __name__ == "__main__": asyncio.run(main())
