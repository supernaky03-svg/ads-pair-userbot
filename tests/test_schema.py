from datetime import datetime, timezone
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import AccessKey, AppUser, Base
from app.services.reporting import pair_quota_done
from types import SimpleNamespace


def test_model_schema_has_single_active_key_guard():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    indexes = inspect(engine).get_indexes("app_access_keys")
    assert any(i["name"] == "uq_app_access_key_one_active_per_user" and i["unique"] for i in indexes)
    with Session(engine) as session:
        user = AppUser(telegram_id=987654321, is_admin=False)
        session.add(user)
        session.flush()
        session.add(AccessKey(user_id=user.id, key_hash="a" * 64, active=True))
        session.commit()
        session.add(AccessKey(user_id=user.id, key_hash="b" * 64, active=True))
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
        else:
            raise AssertionError("the DB should reject multiple active keys for one user")
        key = session.query(AccessKey).filter_by(user_id=user.id, active=True).one()
        key.active = False
        key.revoked_at = datetime.now(timezone.utc)
        session.add(AccessKey(user_id=user.id, key_hash="c" * 64, active=True))
        session.commit()
        assert session.query(AccessKey).filter_by(user_id=user.id, active=True).count() == 1
    engine.dispose()


def test_smart_report_quota_requires_count_and_completion():
    pair = SimpleNamespace(post_count=2)
    assert not pair_quota_done(pair, SimpleNamespace(forwarded_count=2, completed_at=None))
    assert not pair_quota_done(pair, SimpleNamespace(forwarded_count=0, completed_at=datetime.now(timezone.utc)))
    assert pair_quota_done(pair, SimpleNamespace(forwarded_count=2, completed_at=datetime.now(timezone.utc)))


def test_pair_input_rejects_unsupported_overnight_window():
    from pydantic import ValidationError
    from app.web.api import PairInput
    base = {"name": "test", "source_channel": "@source", "target_channel": "@target"}
    try:
        PairInput(**base, start_time="22:00", cutoff_time="08:00")
    except ValidationError:
        pass
    else:
        raise AssertionError("overnight schedule should be rejected")
    assert PairInput(**base, start_time="18:00", cutoff_time=None).start_time == "18:00"


def test_source_and_target_same_channel_is_rejected_before_permissions_check():
    import asyncio
    from types import SimpleNamespace
    from app.services.forwarding import ForwardingEngine

    class FakeTelegram:
        connected = True
        async def resolve_chat(self, value):
            return SimpleNamespace(chat_id=-100123, title="same", username="same")
        async def assert_target_admin(self, target):
            raise AssertionError("permission check should not run for identical channels")

    engine = ForwardingEngine(db=object(), telegram=FakeTelegram(), bot=None, settings=object())
    try:
        asyncio.run(engine.validate_channels("@same", "@same"))
    except ValueError as exc:
        assert "different" in str(exc)
    else:
        raise AssertionError("same source and target should be rejected")


def test_report_user_outbox_is_durable_and_unique_per_pair_day():
    from app.db.models import Base, PairReportOutbox
    table = Base.metadata.tables["pair_report_outbox"]
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("pair_id", "report_date") in unique_columns
    assert "body" in table.c and "last_attempt_at" in table.c and "attempt_count" in table.c
