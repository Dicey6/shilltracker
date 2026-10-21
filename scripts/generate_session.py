"""Create a Telethon StringSession on YOUR computer (safest option).

Run:  python scripts/generate_session.py
The session string is printed to YOUR terminal only. Paste it into the TELETHON_SESSION secret on Render.
Never paste it into a chat, a commit, or a screenshot: it grants full access to your Telegram account.
"""
import asyncio
import getpass
import os

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main() -> None:
    api_id = int(os.environ.get("TELEGRAM_API_ID") or input("TELEGRAM_API_ID: ").strip())
    api_hash = os.environ.get("TELEGRAM_API_HASH") or getpass.getpass("TELEGRAM_API_HASH (hidden): ").strip()
    async with TelegramClient(StringSession(), api_id, api_hash) as client:   # prompts for phone, code, 2FA
        me = await client.get_me()
        print(f"\nLogged in as {me.first_name} (id {me.id}).")
        print("\n--- TELETHON_SESSION (keep secret) ---")
        print(client.session.save())
        print("--------------------------------------\n")


if __name__ == "__main__":
    asyncio.run(main())
