"""
comfy_extras/telemetry_routes.py
Aiohttp routes for the VectorFlow telemetry pipeline.
Currently handles browser console relay → Redis list.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import redis
from aiohttp import web

_log = logging.getLogger("VF.telemetry")

_DEFAULT_KEY    = "vktrflo:console"
_MAX_LIST_LEN   = 50_000
_redis: redis.Redis | None = None


def _get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
    return _redis


def _push_batch(key: str, entries: list[dict[str, Any]]) -> int:
    r = _get_redis()
    pipe = r.pipeline()
    for entry in entries:
        pipe.rpush(key, json.dumps(entry))
    pipe.ltrim(key, -_MAX_LIST_LEN, -1)
    pipe.execute()
    return len(entries)


def register(routes: web.RouteTableDef) -> None:
    @routes.post("/telemetry/console")
    async def console_relay(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400, text="invalid json")

        entries: list[dict] = body.get("entries", [])
        key: str = body.get("key", _DEFAULT_KEY)

        if not entries:
            return web.Response(status=204)

        # Stamp server-side receive time on each entry
        now = time.time()
        for e in entries:
            e.setdefault("server_ts", now)

        try:
            n = _push_batch(key, entries)
            return web.json_response({"ok": True, "pushed": n})
        except Exception as exc:
            _log.error("Redis push failed: %s", exc)
            return web.Response(status=503, text="redis unavailable")
