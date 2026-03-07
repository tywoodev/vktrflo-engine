from __future__ import annotations

import json
import os
from collections import defaultdict, deque
from itertools import count
from typing import Any, Callable, Protocol


class CacheProvider(Protocol):
    def append_entries(self, stream: str, entries: list[dict]) -> int: ...
    def read_entries(self, stream: str, limit: int) -> list[dict[str, Any]]: ...
    def read_entries_after(self, stream: str, after: int, limit: int) -> list[dict[str, Any]]: ...
    def current_cursor(self, stream: str) -> int: ...


class NoopCacheProvider:
    def append_entries(self, stream: str, entries: list[dict]) -> int:
        return len(entries)

    def read_entries(self, stream: str, limit: int) -> list[dict[str, Any]]:
        return []

    def read_entries_after(self, stream: str, after: int, limit: int) -> list[dict[str, Any]]:
        return []

    def current_cursor(self, stream: str) -> int:
        return 0


class MemoryCacheProvider:
    def __init__(self, max_entries: int) -> None:
        self.max_entries = max_entries
        self._streams: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=max_entries)
        )
        self._counters: dict[str, count] = defaultdict(lambda: count(1))

    def _stamp_entry(self, stream: str, entry: dict[str, Any]) -> dict[str, Any]:
        stamped = dict(entry)
        stamped["cursor"] = next(self._counters[stream])
        return stamped

    def append_entries(self, stream: str, entries: list[dict]) -> int:
        bucket = self._streams[stream]
        for entry in entries:
            bucket.append(self._stamp_entry(stream, entry))
        return len(entries)

    def read_entries(self, stream: str, limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        bucket = self._streams.get(stream)
        if not bucket:
            return []
        return [dict(entry) for entry in list(bucket)[-limit:]]

    def read_entries_after(self, stream: str, after: int, limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        bucket = self._streams.get(stream)
        if not bucket:
            return []
        entries = [dict(entry) for entry in bucket if int(entry.get("cursor", 0)) > after]
        return entries[:limit]

    def current_cursor(self, stream: str) -> int:
        bucket = self._streams.get(stream)
        if not bucket:
            return 0
        return int(bucket[-1].get("cursor", 0))


class RedisListCacheProvider:
    def __init__(self, namespace: str, max_entries: int) -> None:
        import redis

        url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379")
        self.namespace = namespace
        self.max_entries = max_entries
        self._client = redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        self._client.ping()

    def _stream_key(self, stream: str) -> str:
        return f"{self.namespace}:{stream}"

    def _cursor_key(self, stream: str) -> str:
        return f"{self.namespace}:{stream}:cursor"

    def _stamp_entry(self, stream: str, entry: dict[str, Any]) -> dict[str, Any]:
        stamped = dict(entry)
        stamped["cursor"] = int(self._client.incr(self._cursor_key(stream)))
        return stamped

    def append_entries(self, stream: str, entries: list[dict]) -> int:
        key = self._stream_key(stream)
        pipe = self._client.pipeline()
        for entry in entries:
            pipe.rpush(key, json.dumps(self._stamp_entry(stream, entry), ensure_ascii=False))
        pipe.ltrim(key, -self.max_entries, -1)
        pipe.execute()
        return len(entries)

    def read_entries(self, stream: str, limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []

        key = self._stream_key(stream)
        raw_entries = self._client.lrange(key, -limit, -1)
        entries: list[dict[str, Any]] = []
        for raw in raw_entries:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                entries.append(parsed)
        return entries

    def read_entries_after(self, stream: str, after: int, limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []

        key = self._stream_key(stream)
        raw_entries = self._client.lrange(key, 0, -1)
        entries: list[dict[str, Any]] = []
        for raw in raw_entries:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            if int(parsed.get("cursor", 0)) <= after:
                continue
            entries.append(parsed)
            if len(entries) >= limit:
                break
        return entries

    def current_cursor(self, stream: str) -> int:
        value = self._client.get(self._cursor_key(stream))
        if value is None:
            return 0
        try:
            return int(value)
        except ValueError:
            return 0


_PROVIDER_FACTORIES: dict[str, Callable[[str, int], CacheProvider]] = {
    "none": lambda _namespace, _max_entries: NoopCacheProvider(),
    "memory": lambda _namespace, max_entries: MemoryCacheProvider(max_entries),
    "redis": lambda namespace, max_entries: RedisListCacheProvider(namespace, max_entries),
}

_provider: CacheProvider | None = None
_provider_name: str | None = None


def register_provider_factory(name: str, factory: Callable[[str, int], CacheProvider]) -> None:
    _PROVIDER_FACTORIES[name] = factory


def _default_provider_name() -> str:
    configured = os.environ.get("VF_CACHE_PROVIDER")
    if configured:
        return configured.strip().lower()
    if os.environ.get("REDIS_URL"):
        return "redis"
    return "memory"


def get_provider_name() -> str:
    global _provider_name
    if _provider_name is None:
        _provider_name = _default_provider_name()
    return _provider_name


def get_provider() -> CacheProvider:
    global _provider
    if _provider is not None:
        return _provider

    provider_name = get_provider_name()
    namespace = os.environ.get("VF_CACHE_NAMESPACE", "vktrflo")
    try:
        max_entries = int(os.environ.get("VF_CACHE_MAX_ENTRIES", "50000"))
    except ValueError:
        max_entries = 50000

    factory = _PROVIDER_FACTORIES.get(provider_name)
    if factory is None:
        raise ValueError(f"unknown cache provider '{provider_name}'")

    _provider = factory(namespace, max_entries)
    return _provider


def append_entries(stream: str, entries: list[dict]) -> int:
    if not entries:
        return 0

    try:
        return get_provider().append_entries(stream, entries)
    except Exception:
        return 0


def read_entries(stream: str, limit: int = 200) -> list[dict[str, Any]]:
    try:
        return get_provider().read_entries(stream, max(0, limit))
    except Exception:
        return []


def read_streams(streams: list[str], limit: int = 200) -> list[dict[str, Any]]:
    if not streams:
        return []

    merged: list[dict[str, Any]] = []
    for stream in streams:
        for entry in read_entries(stream, limit):
            item = dict(entry)
            item.setdefault("stream", stream)
            merged.append(item)

    merged.sort(key=lambda entry: str(entry.get("ts", "")))
    return merged[-limit:]


def read_streams_after(
    streams: list[str],
    after: dict[str, int] | None = None,
    limit: int = 200,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if not streams:
        return [], {}

    provider = get_provider()
    after = after or {}
    merged: list[dict[str, Any]] = []
    cursors: dict[str, int] = {}

    for stream in streams:
        stream_after = max(0, int(after.get(stream, 0)))
        for entry in provider.read_entries_after(stream, stream_after, limit):
            item = dict(entry)
            item.setdefault("stream", stream)
            merged.append(item)
        cursors[stream] = provider.current_cursor(stream)

    merged.sort(key=lambda entry: (int(entry.get("cursor", 0)), str(entry.get("ts", ""))))
    return merged[:limit], cursors
