from aiohttp import web
import asyncio
import json
from typing import Optional
from folder_paths import folder_names_and_paths, get_directory_by_type
from api_server.services.terminal_service import TerminalService
import app.logger
import app.log_cache
import os


def _normalize_cached_entry(entry: dict) -> dict:
    normalized = dict(entry)

    if "message" not in normalized and "msg" in normalized:
        normalized["message"] = str(normalized.get("msg", ""))

    ts = normalized.get("ts")
    if ts is not None and not isinstance(ts, str):
        normalized["ts"] = str(ts)

    return normalized


def _parse_streams(request: web.Request) -> list[str]:
    stream_values = list(request.query.getall("stream", []))
    if not stream_values:
        streams_param = request.query.get("streams", "")
        stream_values = [part.strip() for part in streams_param.split(",") if part.strip()]
    if not stream_values:
        stream_values = ["engine.logs", "browser.console"]
    return stream_values


def _parse_limit(request: web.Request, key: str = "limit", default: int = 200) -> int:
    try:
        return max(1, min(int(request.query.get(key, str(default))), 1000))
    except ValueError:
        return default


def _parse_after_values(request: web.Request, streams: list[str]) -> dict[str, int]:
    after_values: dict[str, int] = {}
    for stream in streams:
        raw_after = request.query.get(f"after[{stream}]")
        if raw_after is None:
            raw_after = request.query.get(f"after.{stream}")
        if raw_after is None:
            continue
        try:
            after_values[stream] = max(0, int(raw_after))
        except ValueError:
            continue
    return after_values


async def _write_sse_event(response: web.StreamResponse, event: str, data: dict) -> None:
    payload = json.dumps(data, ensure_ascii=False)
    await response.write(f"event: {event}\ndata: {payload}\n\n".encode("utf-8"))


class InternalRoutes:
    '''
    The top level web router for internal routes: /internal/*
    The endpoints here should NOT be depended upon. It is for ComfyUI frontend use only.
    Check README.md for more information.
    '''

    def __init__(self, prompt_server):
        self.routes: web.RouteTableDef = web.RouteTableDef()
        self._app: Optional[web.Application] = None
        self.prompt_server = prompt_server
        self.terminal_service = TerminalService(prompt_server)

    def setup_routes(self):
        @self.routes.get('/logs')
        async def get_logs(request):
            return web.json_response("".join([(l["t"] + " - " + l["m"]) for l in app.logger.get_logs()]))

        @self.routes.get('/logs/raw')
        async def get_raw_logs(request):
            self.terminal_service.update_size()
            return web.json_response({
                "entries": list(app.logger.get_logs()),
                "size": {"cols": self.terminal_service.cols, "rows": self.terminal_service.rows}
            })

        @self.routes.get('/logs/cache')
        async def get_cached_logs(request):
            stream_values = _parse_streams(request)
            limit = _parse_limit(request)
            after_values = _parse_after_values(request, stream_values)

            entries, cursors = app.log_cache.read_streams_after(
                stream_values,
                after=after_values,
                limit=limit,
            )
            entries = [_normalize_cached_entry(entry) for entry in entries]
            return web.json_response({
                "provider": app.log_cache.get_provider_name(),
                "streams": stream_values,
                "limit": limit,
                "cursors": cursors,
                "entries": entries,
            }, headers={"Access-Control-Allow-Origin": "*"})

        @self.routes.get('/logs/cache/stream')
        async def stream_cached_logs(request: web.Request) -> web.StreamResponse:
            stream_values = _parse_streams(request)
            limit = _parse_limit(request)
            heartbeat_seconds = _parse_limit(request, key="heartbeat", default=15)
            after_values = _parse_after_values(request, stream_values)

            response = web.StreamResponse(
                status=200,
                reason="OK",
                headers={
                    "Content-Type": "text/event-stream",
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "Access-Control-Allow-Origin": "*",
                },
            )
            await response.prepare(request)

            entries, cursors = app.log_cache.read_streams_after(
                stream_values,
                after=after_values,
                limit=limit,
            )
            entries = [_normalize_cached_entry(entry) for entry in entries]
            await _write_sse_event(
                response,
                "logs",
                {
                    "provider": app.log_cache.get_provider_name(),
                    "streams": stream_values,
                    "limit": limit,
                    "cursors": cursors,
                    "entries": entries,
                },
            )

            idle_ticks = 0
            poll_interval = 1.0
            max_idle_ticks = max(1, heartbeat_seconds)

            try:
                while True:
                    await asyncio.sleep(poll_interval)
                    entries, cursors = app.log_cache.read_streams_after(
                        stream_values,
                        after=cursors,
                        limit=limit,
                    )
                    entries = [_normalize_cached_entry(entry) for entry in entries]

                    if entries:
                        idle_ticks = 0
                        await _write_sse_event(
                            response,
                            "logs",
                            {
                                "provider": app.log_cache.get_provider_name(),
                                "streams": stream_values,
                                "limit": limit,
                                "cursors": cursors,
                                "entries": entries,
                            },
                        )
                        continue

                    idle_ticks += 1
                    if idle_ticks >= max_idle_ticks:
                        idle_ticks = 0
                        await response.write(b": heartbeat\n\n")
            except (asyncio.CancelledError, ConnectionResetError, RuntimeError):
                return response

        @self.routes.patch('/logs/subscribe')
        async def subscribe_logs(request):
            json_data = await request.json()
            client_id = json_data["clientId"]
            enabled = json_data["enabled"]
            if enabled:
                self.terminal_service.subscribe(client_id)
            else:
                self.terminal_service.unsubscribe(client_id)

            return web.Response(status=200)


        @self.routes.get('/folder_paths')
        async def get_folder_paths(request):
            response = {}
            for key in folder_names_and_paths:
                response[key] = folder_names_and_paths[key][0]
            return web.json_response(response)

        @self.routes.get('/files/{directory_type}')
        async def get_files(request: web.Request) -> web.Response:
            directory_type = request.match_info['directory_type']
            if directory_type not in ("output", "input", "temp"):
                return web.json_response({"error": "Invalid directory type"}, status=400)

            directory = get_directory_by_type(directory_type)

            def is_visible_file(entry: os.DirEntry) -> bool:
                """Filter out hidden files (e.g., .DS_Store on macOS)."""
                return entry.is_file() and not entry.name.startswith('.')

            sorted_files = sorted(
                (entry for entry in os.scandir(directory) if is_visible_file(entry)),
                key=lambda entry: -entry.stat().st_mtime
            )
            return web.json_response([entry.name for entry in sorted_files], status=200)


    def get_app(self):
        if self._app is None:
            self._app = web.Application()
            self.setup_routes()
            self._app.add_routes(self.routes)
        return self._app
