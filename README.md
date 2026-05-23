# Session Pair Userbot

Telegram session-account-only userbot for daily pair forwarding.

It reads commands from Saved Messages or a private control group, scans configured source channels, forwards new posts to target channels with the original Telegram **forward header**, pins forwarded target posts, and sends a Day report to the configured username.

## Main behavior

For each pair:

```text
Source channel -> Target channel -> Report user
```

Each pair has its own `post_count`.

Example: `post_count = 2`

```text
09:00 Asia/Yangon: scan pair
- If 2 new posts exist: forward both, pin both, report immediately, stop scanning this pair until tomorrow.
- If only 1 new post exists: forward and pin it, but do not report yet.
- Then retry only this incomplete pair every 1 hour.
- When the second post appears: forward and pin it, report both links, stop until tomorrow.
- If 23:00 arrives and only 1 post exists: report that 1 post and stop until tomorrow.
```

Default values:

```text
TIMEZONE=Asia/Yangon
DAILY_RUN_TIME=09:00
RETRY_INTERVAL_MINUTES=60
CUTOFF_TIME=23:00
JOB_DELAY_SECONDS=5
```

The scheduler processes pairs sequentially, not in parallel. Between pair jobs there is a 5-second delay. After forwarding/pinning and before reporting to `@username`, it also waits 5 seconds.

## Report format

One post:

```text
Day3
https://t.me/target_channel
https://t.me/target_channel/123
```

Multiple posts:

```text
Day3
https://t.me/target_channel

1. https://t.me/target_channel/123
2. https://t.me/target_channel/124
```

## Features

- No bot token required
- Telethon session account only
- Control by Saved Messages or group
- Multi-pair support
- Same source/target/user can be reused in different pairs
- Per-pair `post_count`, default `1`
- Daily scan at 09:00 Myanmar/Yangon time by default
- Incomplete pairs retry every 1 hour
- 23:00 cutoff partial report if at least one post was forwarded
- NeonDB/Postgres storage
- Render web service support
- UptimeRobot `/healthz` endpoint
- Forward new posts only after pair creation
- Forward mode preserves the source channel shown above the target post
- Pin every forwarded post by default
- first_day support
- monthly reset support

## Required target permissions

The session account must be admin in each target channel with:

- Post Messages
- Pin Messages

For private source channels, the session account must be a member.

## Commands

```text
/help
/status
/addpair @username source_link target_link first_day [post_count]
/listpairs
/pair pair_id
/dailyprogress
/delpair pair_id
/pausepair pair_id
/resumepair pair_id
/runpair pair_id
/runall
/edituser pair_id @newusername
/editsource pair_id new_source_link
/edittarget pair_id new_target_link
/resetday pair_id day_number
/setfirstday pair_id first_day
/setpostcount pair_id count
/settime HH:MM
/setpinmode pair_id all|none
/testnotify pair_id
/testforward pair_id
```

## Add pair examples

Default post count is 1:

```text
/addpair @user1 https://t.me/auto388game https://t.me/mytargetchannel 3
```

Post count 2:

```text
/addpair @user1 https://t.me/auto388game https://t.me/mytargetchannel 3 2
```

If pair is created on Jan 22 and `first_day = 3`, the first report is Day3 and the monthly cycle resets to Day1 on Feb 19.

## Generate session string

Run locally, not on Render:

```bash
pip install -r requirements.txt
python scripts/generate_session.py
```

Paste the printed string into Render as `TELETHON_SESSION_STRING`.

## Render setup

1. Create NeonDB database.
2. Create a Render Web Service from this repo/zip.
3. Set environment variables from `.env.example`.
4. Start command:

```bash
python -m app.main
```

5. Add UptimeRobot monitor:

```text
https://your-render-service.onrender.com/healthz
```

## Notes

- If the source channel has protected content enabled, Telegram may block forwarding.
- If the report user privacy prevents DMs, the error is sent to the control chat and stored in DB.
- Private channel post links use `https://t.me/c/.../...` and only work for channel members.
- If more new posts exist than the pair `post_count`, the extra posts are left for the next daily cycle because `last_seen_message_id` is advanced only to the last forwarded source post.
