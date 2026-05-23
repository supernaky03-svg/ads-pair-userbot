from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from aiohttp import web


class HealthServer:
    def __init__(self, *, host: str, port: int, path: str) -> None:
        self.host = host
        self.port = port
        self.path = path
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get(self.path, self._health, allow_head=True)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port)
        await self.site.start()

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "service": "session-pair-userbot",
                "time_utc": datetime.now(timezone.utc).isoformat(),
            }
        )
