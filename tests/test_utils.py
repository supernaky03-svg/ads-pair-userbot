from datetime import date
from app.utils import key_hash, new_access_key, valid_hhmm, initial_next_reset_date, create_session_token, read_session_token, report_retry_delay

def test_access_key_is_random_and_hashable():
    a, b = new_access_key(), new_access_key()
    assert a.startswith("apk_live_") and a != b
    assert len(key_hash(a)) == 64

def test_hhmm_validation():
    assert valid_hhmm("09:00") and valid_hhmm("23:59")
    assert not valid_hhmm("24:00") and not valid_hhmm("9:00")

def test_first_day_cycle_date():
    assert initial_next_reset_date(date(2026, 1, 22), 3) == date(2026, 2, 19)

def test_signed_session_token_roundtrip():
    decoded = read_session_token(create_session_token(12, 34, "test-secret"), "test-secret")
    assert decoded["uid"] == 12 and decoded["kid"] == 34

def test_invalid_session_signature_rejected():
    token = create_session_token(12, 34, "test-secret")
    try: read_session_token(token, "wrong-secret")
    except ValueError: pass
    else: raise AssertionError("bad signature should be rejected")

def test_session_token_expiry_and_identity_are_signed():
    token = create_session_token(50, 80, "another-test-secret")
    decoded = read_session_token(token, "another-test-secret")
    assert decoded["uid"] == 50 and decoded["kid"] == 80
    try:
        read_session_token(token + "x", "another-test-secret")
    except ValueError:
        pass
    else:
        raise AssertionError("tampered token should be rejected")


def test_report_retry_uses_bounded_exponential_backoff():
    assert report_retry_delay(300, 1) == 300
    assert report_retry_delay(300, 2) == 600
    assert report_retry_delay(300, 20) == 21600

def test_telegram_init_data_signature_is_verified():
    import hashlib, hmac, json, time
    from urllib.parse import urlencode
    from app.utils import validate_telegram_init_data
    token = "12345:fake-token-for-tests"
    values = {
        "auth_date": str(int(time.time())),
        "query_id": "AAE-test-query",
        "user": json.dumps({"id": 123456789, "first_name": "Tester"}, separators=(",", ":")),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(values.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    payload = urlencode(values)
    assert validate_telegram_init_data(payload, token)["telegram_id"] == 123456789
    try:
        validate_telegram_init_data(payload + "x", token)
    except (ValueError, KeyError):
        pass
    else:
        raise AssertionError("tampered initData should be rejected")


def test_telegram_init_data_rejects_future_timestamp():
    import hashlib, hmac, json, time
    from urllib.parse import urlencode
    from app.utils import validate_telegram_init_data
    token = "12345:fake-token-for-tests"
    values = {
        "auth_date": str(int(time.time()) + 3600),
        "query_id": "AAE-test-query",
        "user": json.dumps({"id": 123456789, "first_name": "Tester"}, separators=(",", ":")),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(values.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    try:
        validate_telegram_init_data(urlencode(values), token)
    except ValueError:
        pass
    else:
        raise AssertionError("future timestamp should be rejected")


def test_channel_links_are_canonical_for_public_and_private_targets():
    from app.utils import channel_link_for_chat, channel_post_link
    assert channel_link_for_chat(-1001234567890, "@target") == "https://t.me/target"
    assert channel_link_for_chat(-1001234567890, None) == "https://t.me/c/1234567890"
    assert channel_post_link(-1001234567890, None, 42) == "https://t.me/c/1234567890/42"
