from __future__ import annotations
import ssl
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from app.db.models import Base
from app.config import Settings

class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        connect_args = {}
        if settings.database_url.startswith("postgresql+asyncpg://") and settings.database_ssl:
            connect_args["ssl"] = ssl.create_default_context()
        engine_options = {"future": True, "connect_args": connect_args}
        # The original project intentionally uses NullPool for Neon scale-to-zero.
        if settings.database_url.startswith("postgresql+"):
            engine_options["poolclass"] = NullPool
        self.engine: AsyncEngine = create_async_engine(settings.database_url, **engine_options)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            if conn.dialect.name == "postgresql":
                # Backward-compatible migration for databases created by the original repo.
                await conn.execute(text("ALTER TABLE pairs ADD COLUMN IF NOT EXISTS owner_user_id INTEGER REFERENCES app_users(id) ON DELETE SET NULL"))
                await conn.execute(text("ALTER TABLE pairs ADD COLUMN IF NOT EXISTS pair_name VARCHAR(120) NOT NULL DEFAULT ''"))
                pin_count_exists = bool(await conn.scalar(text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = 'pairs' AND column_name = 'pin_count')"
                )))
                await conn.execute(text("ALTER TABLE pairs ADD COLUMN IF NOT EXISTS pin_count INTEGER NOT NULL DEFAULT 1"))
                # The legacy engine pinned every forwarded post when pin_mode='all'.
                # On first migration only, map that behavior to post_count; don't overwrite user choices later.
                if not pin_count_exists:
                    await conn.execute(text("UPDATE pairs SET pin_count = GREATEST(1, post_count) WHERE pin_mode <> 'none'"))
                await conn.execute(text("ALTER TABLE pairs ADD COLUMN IF NOT EXISTS start_time VARCHAR(5)"))
                await conn.execute(text("ALTER TABLE pairs ADD COLUMN IF NOT EXISTS cutoff_time VARCHAR(5)"))
                await conn.execute(text("ALTER TABLE pairs ALTER COLUMN report_username DROP NOT NULL"))
                # Enforce one active access key per user even if two rotate requests race.
                await conn.execute(text("UPDATE app_access_keys SET active = FALSE, revoked_at = COALESCE(revoked_at, created_at) WHERE active IS TRUE AND id NOT IN (SELECT MAX(id) FROM app_access_keys WHERE active IS TRUE GROUP BY user_id)"))
                await conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_app_access_key_one_active_per_user ON app_access_keys (user_id) WHERE active IS TRUE"))
                await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_pairs_owner_active ON pairs (owner_user_id, active)"))
                await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_forwarded_pair_day ON forwarded_posts (pair_id, run_date)"))
                await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_daily_pair_progress_run_date ON daily_pair_progress (run_date)"))
                await conn.execute(text("ALTER TABLE smart_reports ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMPTZ"))
                await conn.execute(text("ALTER TABLE smart_reports ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0"))
                await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_smart_reports_last_attempt_at ON smart_reports (last_attempt_at)"))
            elif conn.dialect.name == "sqlite":
                # Fresh local databases already have these fields. Existing SQLite imports should be upgraded separately.
                pass

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def close(self) -> None:
        await self.engine.dispose()
