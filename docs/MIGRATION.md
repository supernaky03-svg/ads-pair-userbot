# Migration from the public `ads-pair-userbot` repository

This package is an integration rebuild based on the published repository's pair-cycle behavior and schema. It keeps the important concepts (`pairs`, `daily_pair_progress`, `forwarded_posts`, `report_logs`, `post_count`, `first_day/current_day`, monthly reset dates, new-post-only checkpoints, retry interval, cutoff partial reports, and Telethon forwarding with source headers). It adds a user/key layer and a Bot API Mini App.

## Before moving the live database

1. Make a Neon backup/snapshot or export the current database.
2. Deploy to a **separate test service** and preferably a copied test database first.
3. Set `LEGACY_OWNER_TELEGRAM_ID` to the Telegram ID that should own current unassigned pairs.
4. Startup migration adds `owner_user_id`, `pair_name`, `pin_count`, `start_time`, and `cutoff_time`; it also allows `report_username` to be NULL. Existing rows without an owner are assigned to the configured legacy owner.
5. Use the same `TELETHON_SESSION_STRING` only in one live instance. Two copies of a user session and scheduler can cause duplicate sends.
6. Report User messages use a separate durable outbox; delivery failure no longer blocks the completed forwarding cycle or owner Smart Report. Outbox entries expire after seven days.
7. Test permissions, quota, pinning, Report User, Smart Reports, key rotation, and restart recovery with test channels before cutover.

## Scope note

The repository host was readable through the web viewer, but this execution environment could not `git clone` the repo into the workspace. Therefore this ZIP is a self-contained integration rebuild using the original repo's published data model and documented behavior; it is **not a byte-for-byte modified clone**. The Mini App is the primary Pair-management surface in this package. The original Telethon Saved Messages/private-control-group command router is not copied verbatim; the BotFather bot handles `/start`, `/key`, `/revoke`, `/status`, and admin `/createuser`, with an admin Users tab in the Mini App. Telethon remains the forwarding sender. Review custom/local patches in the original before replacing it.
