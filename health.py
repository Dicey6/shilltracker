"""Tiny HTTP server for Render's health check. Exposes liveness only - never account data."""
from __future__ import annotations

import logging
from typing import Callable

from aiohttp import web

log = logging.getLogger(__name__)


class HealthServer:
    def __init__(self, port: int, snapshot: Callable[[], dict]):
        self.port, self.snapshot = port, snapshot
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/healthz", self._health)
        app.router.add_get("/", self._health)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, "0.0.0.0", self.port).start()
        log.info("Health endpoint listening on :%s/healthz", self.port)

    async def _health(self, _: web.Request) -> web.Response:
        return web.json_response(self.snapshot())

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
