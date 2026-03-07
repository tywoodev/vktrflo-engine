"""
comfy_extras/telemetry_routes.py
Aiohttp routes for the VectorFlow telemetry pipeline.
Currently handles browser console relay → configured cache provider.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from aiohttp import web
from app.log_cache import append_entries

_log = logging.getLogger("VF.telemetry")
_DEFAULT_STREAM = "browser.console"


def register(routes: web.RouteTableDef) -> None:
    @routes.post("/telemetry/console")
    async def console_relay(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400, text="invalid json")

        entries: list[dict] = body.get("entries", [])
        stream: str = body.get("key", _DEFAULT_STREAM)

        if not entries:
            return web.Response(status=204)

        # Stamp server-side receive time on each entry
        now = time.time()
        for e in entries:
            e.setdefault("server_ts", now)
            e.setdefault("source", "engine.browser")

        try:
            n = append_entries(stream, entries)
            return web.json_response({"ok": True, "pushed": n})
        except Exception as exc:
            _log.error("Cache push failed: %s", exc)
            return web.Response(status=503, text="cache unavailable")
