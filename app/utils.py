from __future__ import annotations
from datetime import date, timedelta
import hashlib, hmac, json, secrets, time, base64
from urllib.parse import parse_qsl

def key_hash(raw: str) -> str: return hashlib.sha256(raw.encode("utf-8")).hexdigest()
def new_access_key() -> str: return "apk_live_" + secrets.token_urlsafe(32)
def valid_hhmm(value: str) -> bool:
    try:
        if len(value) != 5 or value[2] != ":": return False
        h, m = map(int, value.split(":")); return 0 <= h <= 23 and 0 <= m <= 59
    except Exception: return False

def initial_next_reset_date(today: date, first_day: int) -> date:
    return today + timedelta(days=max(1, 31 - max(1, min(31, first_day))))

def apply_monthly_reset(today: date, current_day: int, next_reset_date: date) -> tuple[int, date, bool]:
    if today < next_reset_date: return current_day, next_reset_date, False
    # Preserve the repo's monthly-cycle idea: reset to Day1 after the configured cycle boundary.
    next_reset = next_reset_date
    while next_reset <= today:
        month = next_reset.month + 1
        year = next_reset.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        import calendar
        next_reset = date(year, month, min(next_reset.day, calendar.monthrange(year, month)[1]))
    return 1, next_reset, True

def validate_telegram_init_data(init_data: str, bot_token: str, max_age_seconds: int = 86400) -> dict:
    if not bot_token: raise ValueError("Bot token is not configured")
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", "")
    auth_date = int(values.get("auth_date", "0"))
    now = int(time.time())
    if not received_hash or auth_date <= 0 or auth_date > now + 60 or (now - auth_date) > max_age_seconds:
        raise ValueError("Telegram initData is missing, expired or from the future")
    check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash): raise ValueError("Invalid Telegram initData signature")
    user = json.loads(values["user"])
    return {"telegram_id": int(user["id"]), "first_name": user.get("first_name", "")}

def create_session_token(user_id: int, key_id: int, secret: str, lifetime: int = 43200) -> str:
    payload = {"uid": user_id, "kid": key_id, "exp": int(time.time()) + lifetime}
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return raw + "." + sig

def read_session_token(token: str, secret: str) -> dict:
    try:
        raw, sig = token.split(".", 1)
        expected = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig): raise ValueError("bad signature")
        payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        if int(payload["exp"]) < int(time.time()): raise ValueError("expired")
        return payload
    except Exception as exc: raise ValueError("Invalid or expired session") from exc

def channel_link_for_chat(chat_id: int, username: str | None) -> str:
    """Build a display/report link without replacing the user's editable channel input."""
    if username: return f"https://t.me/{username.lstrip('@')}"
    ident = str(abs(int(chat_id)))
    if ident.startswith("100"): ident = ident[3:]
    return f"https://t.me/c/{ident}"

def channel_post_link(chat_id: int, username: str | None, message_id: int) -> str:
    if username: return f"https://t.me/{username.lstrip('@')}/{message_id}"
    base = channel_link_for_chat(chat_id, username)
    return f"{base}/{message_id}"


def report_retry_delay(base_seconds: int, attempt_count: int, max_seconds: int = 21600) -> int:
    """Exponential retry backoff to keep a permanently undeliverable report from burning DB quota."""
    base = max(120, int(base_seconds))
    exponent = max(0, min(7, int(attempt_count or 1) - 1))
    return min(base * (2 ** exponent), max_seconds)
