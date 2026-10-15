"""Compatibility note.
The v2 interface preserves the public command/control concept of the original repo, but the original
in-chat command router is not embedded as a second command parser in the new Bot API. The Telethon
account remains the sole forwarding/pinning sender. The BotFather bot is the Mini App, key, and Smart
Reporting control plane. See MIGRATION.md before replacing an existing deployment.
"""
