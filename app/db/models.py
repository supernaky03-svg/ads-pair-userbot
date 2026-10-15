from __future__ import annotations
from datetime import date, datetime, timezone
from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Index, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

class Base(DeclarativeBase): pass

def utcnow() -> datetime: return datetime.now(timezone.utc)

class AppUser(Base):
    __tablename__ = "app_users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    pairs: Mapped[list["Pair"]] = relationship(back_populates="owner")

class AccessKey(Base):
    __tablename__ = "app_access_keys"
    __table_args__ = (
        Index(
            "uq_app_access_key_one_active_per_user", "user_id", unique=True,
            postgresql_where=text("active IS TRUE"),
            sqlite_where=text("active = 1"),
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_users.id", ondelete="CASCADE"), index=True, nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class Pair(Base):
    """Pair model keeps the original repo's field names and daily-cycle semantics."""
    __tablename__ = "pairs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_users.id", ondelete="SET NULL"), index=True, nullable=True)
    pair_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    report_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_input: Mapped[str] = mapped_column(Text, nullable=False)
    source_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_input: Mapped[str] = mapped_column(Text, nullable=False)
    target_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_day: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    current_day: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    next_reset_date: Mapped[date] = mapped_column(Date, nullable=False)
    post_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_seen_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    pin_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="all")
    pin_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    start_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    cutoff_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner: Mapped[AppUser | None] = relationship(back_populates="pairs")
    forwarded_posts: Mapped[list["ForwardedPost"]] = relationship(back_populates="pair", cascade="all, delete-orphan", passive_deletes=True)
    report_logs: Mapped[list["ReportLog"]] = relationship(back_populates="pair", cascade="all, delete-orphan", passive_deletes=True)
    report_outbox: Mapped[list["PairReportOutbox"]] = relationship(back_populates="pair", cascade="all, delete-orphan", passive_deletes=True)
    daily_progress: Mapped[list["DailyPairProgress"]] = relationship(back_populates="pair", cascade="all, delete-orphan", passive_deletes=True)
    __table_args__ = (Index("ix_pairs_owner_active", "owner_user_id", "active"),)

class DailyPairProgress(Base):
    __tablename__ = "daily_pair_progress"
    __table_args__ = (UniqueConstraint("pair_id", "run_date", name="uq_daily_pair_progress_once"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pair_id: Mapped[int] = mapped_column(ForeignKey("pairs.id", ondelete="CASCADE"), nullable=False, index=True)
    run_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    day_number: Mapped[int] = mapped_column(Integer, nullable=False)
    required_post_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    forwarded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_forwarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    pair: Mapped[Pair] = relationship(back_populates="daily_progress")

class ForwardedPost(Base):
    __tablename__ = "forwarded_posts"
    __table_args__ = (UniqueConstraint("pair_id", "source_chat_id", "source_message_id", name="uq_forwarded_source_once"),
                      Index("ix_forwarded_pair_day", "pair_id", "run_date"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pair_id: Mapped[int] = mapped_column(ForeignKey("pairs.id", ondelete="CASCADE"), nullable=False, index=True)
    run_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    day_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_post_link: Mapped[str] = mapped_column(Text, nullable=False)
    forwarded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    pair: Mapped[Pair] = relationship(back_populates="forwarded_posts")

class PairReportOutbox(Base):
    """Durable per-pair Report User delivery queue, independent of work completion/Smart Reports."""
    __tablename__ = "pair_report_outbox"
    __table_args__ = (
        UniqueConstraint("pair_id", "report_date", name="uq_pair_report_outbox_once"),
        Index("ix_pair_report_pending_retry", "status", "last_attempt_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pair_id: Mapped[int] = mapped_column(ForeignKey("pairs.id", ondelete="CASCADE"), nullable=False, index=True)
    report_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    day_number: Mapped[int] = mapped_column(Integer, nullable=False)
    report_username: Mapped[str] = mapped_column(String(255), nullable=False)
    channel_link: Mapped[str] = mapped_column(Text, nullable=False)
    post_links: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    pair: Mapped[Pair] = relationship(back_populates="report_outbox")


class ReportLog(Base):
    __tablename__ = "report_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pair_id: Mapped[int] = mapped_column(ForeignKey("pairs.id", ondelete="CASCADE"), nullable=False, index=True)
    day_number: Mapped[int] = mapped_column(Integer, nullable=False)
    report_username: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    channel_link: Mapped[str] = mapped_column(Text, nullable=False)
    post_links: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    pair: Mapped[Pair] = relationship(back_populates="report_logs")

class SmartReport(Base):
    """Persistent report/outbox row; sent content is cleaned after 7 days; pending rows are retained for retry."""
    __tablename__ = "smart_reports"
    __table_args__ = (UniqueConstraint("owner_user_id", "report_date", name="uq_smart_report_user_day"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False, index=True)
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

class AppLog(Base):
    __tablename__ = "app_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    level: Mapped[str] = mapped_column(String(10), nullable=False, default="INFO")
    event: Mapped[str] = mapped_column(String(100), nullable=False)
    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
