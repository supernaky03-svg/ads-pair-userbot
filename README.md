# Ads Pair Userbot v2 — Mini App + Multi-user

Mobile-first Telegram Mini App and Bot API management layer for the [original Session Pair Userbot repository](https://github.com/supernaky03-svg/ads-pair-userbot).

## Core behavior

- BotFather bot opens the dashboard, issues/revokes access keys, and delivers mandatory Smart Reports.
- One Telethon user session remains the sender for all pairs; forwarding preserves the source attribution header.
- Every pair belongs to one permanent app user. API queries scope by owner.
- Login uses Telegram Mini App `initData` plus a personal `apk_live_...` key. The backend validates Telegram's signature and requires the key to belong to that Telegram identity. Only a SHA-256 hash is stored.
- Rotating a key disables the previous key and all sessions based on it. Revoking a key preserves the user account and pairs.
- Pair config: source, target, daily post count (1–50), starting Day, pause/enable, optional start/cutoff times, pin toggle/count (1–10), optional Report User. Source and target must be different chats.
- Schedule defaults are 09:00 Asia/Yangon, hourly retries, and a 23:00 cutoff. A new Pair starts from new source posts (older backlog is skipped). If no post arrives by cutoff, the day's progress is closed but the Day number is not advanced.
- Smart Reporting cannot be disabled: send to the workspace owner when all active pairs meet quota; otherwise send a partial status at cutoff. Report User is separate; blank sends no per-pair report. A failed Report User DM is queued with bounded retries and does not block the pair's completion or Smart Report.
- Logs, per-post forwarding history, per-pair reports and Smart Report content are kept for 7 days. Daily progress older than the 7-day date window is pruned; current-day progress and source checkpoints remain.
- Cleanup runs once daily as batched deletes. Health checks do not query Neon on every ping. The scheduler sleeps until its next due event (daily start, retry, cutoff, or a Mini App change) instead of polling every 120 seconds during normal operation.

## Setup

1. Python 3.11+.
2. Get Telegram API ID/hash and generate a Telethon StringSession locally with `python scripts/generate_session.py`.
3. Create a BotFather bot; its token powers Mini App auth, key commands, and Smart Reports.
4. Create a Neon PostgreSQL database.
5. Copy `.env.example` to `.env` and configure all credentials. On Render, add values in Environment instead of uploading `.env`.
6. Install/run:

   ```bash
   pip install -r requirements.txt
   uvicorn app.web.api:app --host 0.0.0.0 --port $PORT
   ```

7. Set `PUBLIC_BASE_URL` to your HTTPS Render address. Use it as the bot's Mini App URL in BotFather.
8. Set `LEGACY_OWNER_TELEGRAM_ID` before using an existing database; this binds existing unowned pairs to that account.

## Bot commands

- `/start`, `/help` — dashboard and help
- `/key` — create/rotate one-time personal key
- `/revoke` — revoke access key
- `/status` — account pair summary
- `/createuser TELEGRAM_ID` — admin-only account prep and one-time key
- Admins can also create workspace accounts in the Mini App's **Users** tab; an existing active key is not silently replaced.

## Render / Neon / UptimeRobot

- Build: `pip install -r requirements.txt`
- Start: `uvicorn app.web.api:app --host 0.0.0.0 --port $PORT`
- Health endpoint: `/healthz` (no database query on each ping).
- The scheduler wakes for daily start, retry, cutoff, and Mini App pair changes; it does not hold a Neon connection open while idle.
- The Telethon service refuses to start an interactive login prompt in Render. Generate an authorized StringSession locally with `python scripts/generate_session.py`.
- Configure all environment values from `.env.example` in Render.
- UptimeRobot can monitor `/healthz`, but does not guarantee 24/7 uptime. Frequent pings can keep a Free web service awake and consume its runtime quota. The health endpoint avoids a database query to limit extra Neon compute.
- Run only one live service instance using the shared Telethon session.

## 7-day retention

| Data | Retention |
|---|---|
| Database application logs | 7 days |
| Forwarded-post history | 7 days |
| Per-pair report history | 7 days |
| Pending/sent Report User outbox items | 7 days |
| Sent Smart Report content | 7 days |
| Revoked key hashes | 7 days |
| User accounts, active keys, pair settings | Until explicitly removed |
| Source checkpoints and current-day progress | While operationally needed |
| Completed or incomplete daily progress older than 7 calendar dates | Pruned |

Pending Smart Reports are retained while within the 7-day window so transient delivery errors can be retried; any report older than 7 days is removed, including a report that never delivered. Render stdout log retention is managed by Render, not this database cleaner.

## Control interface change

The Mini App is the primary Pair management interface in this v2 package. The legacy Telethon Saved Messages/private-control-group text command router from the original repo is **not copied verbatim** into this integration rebuild; use the BotFather commands above plus the Mini App. The published README's forwarding/cycle behavior is represented in the new engine, but any private/local modifications to upstream source need a manual comparison before replacing the original deployment.

## Important safety notes

- First integration build; test with disposable channels and a copied database before live cutover.
- `LEGACY_OWNER_TELEGRAM_ID` should be set explicitly to prevent existing pairs being assigned to the wrong account.
- New pairs skip old source backlog, matching the original documentation.
- The pin count means the latest N messages from a forwarding batch are pinned; older pins are not automatically removed.
- This v2 web package's Python files are a self-contained rebuild of the documented behavior. The original private/local Telethon command handlers were not carried over byte-for-byte; check `docs/MIGRATION.md` before replacing an existing deployment.
- Telegram does not provide exactly-once transactional forwarding. A process crash between Telegram success and database commit can still cause an edge-case duplicate; checkpoint recovery and unique DB constraints reduce this risk.
- This package uses Bot API for account/dashboard/reporting and Telethon for forwarding.
