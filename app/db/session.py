from __future__ import annotations

import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Base


_SSL_QUERY_KEYS = {
    "sslmode",
    "ssl",
    "sslcert",
    "sslkey",
    "sslrootcert",
    "channel_binding",
}


def _make_asyncpg_url(database_url: str) -> str:
    """Normalize Neon/Postgres URLs for SQLAlchemy asyncpg.

    Neon usually gives a URL like:
        postgresql://user:pass@host/db?sslmode=require

    asyncpg should receive SSL through connect_args, not via libpq-style
    URL parameters. This function converts the driver name and removes SSL
    query parameters that can crash Render startup.
    """
    url = database_url.strip().strip('"').strip("'")

    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]

    parts = urlsplit(url)
    query_items = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _SSL_QUERY_KEYS
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query_items), parts.fragment))


class Database:
    def __init__(self, database_url: str, *, use_ssl: bool = True) -> None:
        safe_url = _make_asyncpg_url(database_url)

        connect_args = {}
        if safe_url.startswith("postgresql+asyncpg://") and use_ssl:
            connect_args["ssl"] = ssl.create_default_context()

        self.engine: AsyncEngine = create_async_engine(
            safe_url,
            pool_pre_ping=True,
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
