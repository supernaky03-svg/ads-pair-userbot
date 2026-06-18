from __future__ import annotations

import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models import Base


class Database:
    def __init__(self, database_url: str, *, use_ssl: bool = True) -> None:
        connect_args = {}
        if database_url.startswith("postgresql+asyncpg://") and use_ssl:
            # Pass SSL directly to asyncpg instead of relying on URL query params like sslmode=require.
            # This avoids Neon/Render startup crashes caused by malformed or unsupported sslmode values.
            connect_args["ssl"] = ssl.create_default_context()

        self.engine: AsyncEngine = create_async_engine(
            database_url,
            # Important for Neon Free/Scale-to-Zero:
            # Do not keep idle database connections open in SQLAlchemy's pool.
            # Each DB operation opens a connection, completes, and closes it so Neon can suspend after inactivity.
            poolclass=NullPool,
            pool_pre_ping=False,
            future=True,
            connect_args=connect_args,
        )
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # Small safe migrations for users upgrading an older zip on the same NeonDB.
            await conn.execute(text("ALTER TABLE pairs ADD COLUMN IF NOT EXISTS post_count INTEGER NOT NULL DEFAULT 1"))
            await conn.execute(text("ALTER TABLE forwarded_posts ADD COLUMN IF NOT EXISTS run_date DATE"))
            await conn.execute(text("ALTER TABLE forwarded_posts ADD COLUMN IF NOT EXISTS day_number INTEGER"))
            await conn.execute(text("UPDATE pairs SET post_count = 1 WHERE post_count IS NULL OR post_count < 1"))
            await conn.execute(text("UPDATE pairs SET pin_mode = 'all' WHERE pin_mode = 'last' OR pin_mode IS NULL"))

    async def close(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
