from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Pair(Base):
    __tablename__ = "pairs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    report_username: Mapped[str] = mapped_column(String(255), nullable=False)

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

    # Daily quota. Example: post_count=2 means forward 2 new source posts for this pair before sending the report.
    post_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    last_seen_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # all = pin every forwarded post, none = do not pin. last is kept for compatibility with old DB rows.
    pin_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="all")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    forwarded_posts: Mapped[list["ForwardedPost"]] = relationship(back_populates="pair", cascade="all, delete-orphan")
    report_logs: Mapped[list["ReportLog"]] = relationship(back_populates="pair", cascade="all, delete-orphan")
    daily_progress: Mapped[list["DailyPairProgress"]] = relationship(back_populates="pair", cascade="all, delete-orphan")


class DailyPairProgress(Base):
    __tablename__ = "daily_pair_progress"
    __table_args__ = (UniqueConstraint("pair_id", "run_date", name="uq_daily_pair_progress_once"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pair_id: Mapped[int] = mapped_column(Integer, ForeignKey("pairs.id", ondelete="CASCADE"), nullable=False)

    # Local date in TIMEZONE, not UTC. For this project default is Asia/Yangon.
    run_date: Mapped[date] = mapped_column(Date, nullable=False)
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
    __table_args__ = (UniqueConstraint("pair_id", "source_chat_id", "source_message_id", name="uq_forwarded_source_once"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pair_id: Mapped[int] = mapped_column(Integer, ForeignKey("pairs.id", ondelete="CASCADE"), nullable=False)

    run_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    day_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    source_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    target_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_post_link: Mapped[str] = mapped_column(Text, nullable=False)

    forwarded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    pair: Mapped[Pair] = relationship(back_populates="forwarded_posts")


class ReportLog(Base):
    __tablename__ = "report_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pair_id: Mapped[int] = mapped_column(Integer, ForeignKey("pairs.id", ondelete="CASCADE"), nullable=False)

    day_number: Mapped[int] = mapped_column(Integer, nullable=False)
    report_username: Mapped[str] = mapped_column(String(255), nullable=False)
    channel_link: Mapped[str] = mapped_column(Text, nullable=False)
    post_links: Mapped[str] = mapped_column(Text, nullable=False)

    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    pair: Mapped[Pair] = relationship(back_populates="report_logs")


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
