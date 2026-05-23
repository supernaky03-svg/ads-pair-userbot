from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Base


class Database:
    def __init__(self, database_url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(database_url, pool_pre_ping=True, future=True)
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
