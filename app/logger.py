from collections import deque
from datetime import datetime
import io
import logging
import sys
from typing import Callable, Iterable
from app.log_cache import append_entries, get_provider_name

logs = None
_flush_callbacks: list[Callable[[list[dict]], None]] = []
_stdout_proxy = None
_stderr_proxy = None


class _BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        if logs is None:
            return

        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()

        entry = {
            "t": datetime.now().isoformat(),
            "m": f"{message}\n",
            "level": record.levelname,
            "logger": record.name,
        }
        logs.append(entry)
        append_entries(
            "engine.logs",
            [{
                "ts": entry["t"],
                "message": message,
                "level": record.levelname,
                "logger": record.name,
                "source": "engine.python",
            }],
        )
        _notify_flush([entry])


class _ConsoleProxy(io.TextIOBase):
    def __init__(self, stream, level: str, source: str):
        self._stream = stream
        self._level = level
        self._source = source
        self._buffer = ""

    def writable(self) -> bool:
        return True

    @property
    def encoding(self):
        return getattr(self._stream, "encoding", None)

    def isatty(self) -> bool:
        return bool(getattr(self._stream, "isatty", lambda: False)())

    def fileno(self) -> int:
        return self._stream.fileno()

    def flush(self) -> None:
        self._flush_buffer(force=True)
        self._stream.flush()

    def write(self, data):
        if not isinstance(data, str):
            data = str(data)

        written = self._stream.write(data)
        self._buffer += data
        self._flush_buffer(force=False)
        return written

    def _flush_buffer(self, force: bool) -> None:
        entries: list[dict] = []
        while True:
            newline_index = self._buffer.find("\n")
            if newline_index < 0:
                break
            line = self._buffer[:newline_index].rstrip("\r")
            self._buffer = self._buffer[newline_index + 1 :]
            if line.strip():
                entries.append(_console_entry(line, self._level, self._source))

        if force and self._buffer.strip():
            entries.append(_console_entry(self._buffer.rstrip("\r"), self._level, self._source))
            self._buffer = ""

        if entries:
            _append_console_entries(entries)


def _console_entry(message: str, level: str, source: str) -> dict:
    return {
        "ts": datetime.now().isoformat(),
        "message": message,
        "level": level,
        "logger": source,
        "source": source,
    }


def _append_console_entries(entries: list[dict]) -> None:
    if not entries:
        return

    if logs is not None:
        for entry in entries:
            logs.append({
                "t": entry["ts"],
                "m": f'{entry["message"]}\n',
                "level": entry["level"],
                "logger": entry["logger"],
            })

    append_entries("engine.logs", entries)
    _notify_flush([
        {
            "t": entry["ts"],
            "m": f'{entry["message"]}\n',
            "level": entry["level"],
            "logger": entry["logger"],
        }
        for entry in entries
    ])


def _notify_flush(entries: Iterable[dict]) -> None:
    batch = list(entries)
    if not batch:
        return

    for cb in _flush_callbacks:
        cb(batch)


def get_logs():
    return logs


def on_flush(callback):
    _flush_callbacks.append(callback)


def setup_logger(log_level: str = "INFO", capacity: int = 300, use_stdout: bool = False):
    global logs, _stdout_proxy, _stderr_proxy
    if logs is not None:
        return

    logs = deque(maxlen=capacity)
    original_stdout = sys.stdout
    original_stderr = sys.stderr

    # Tee raw print()/stderr writes into the cache provider without reintroducing
    # launcher-side stdio interception. Logging handlers still target the original
    # streams so structured logging does not get duplicated through the proxy.
    _stdout_proxy = _ConsoleProxy(original_stdout, "INFO", "engine.stdout")
    _stderr_proxy = _ConsoleProxy(original_stderr, "ERROR", "engine.stderr")
    sys.stdout = _stdout_proxy
    sys.stderr = _stderr_proxy
    try:
        provider_name = get_provider_name()
        print(f"[VKTRFLO Log] cache provider: {provider_name}", file=original_stderr, flush=True)
    except Exception:
        pass

    logger = logging.getLogger()
    logger.setLevel(log_level)

    formatter = logging.Formatter("%(message)s")

    # Prevent duplicate handlers if upstream code reconfigures the root logger.
    logger.handlers.clear()

    buffer_handler = _BufferHandler()
    buffer_handler.setFormatter(formatter)
    logger.addHandler(buffer_handler)

    if use_stdout:
        stdout_handler = logging.StreamHandler(original_stdout)
        stdout_handler.setFormatter(formatter)
        stdout_handler.addFilter(lambda record: record.levelno < logging.ERROR)
        logger.addHandler(stdout_handler)

        stderr_handler = logging.StreamHandler(original_stderr)
        stderr_handler.setFormatter(formatter)
        stderr_handler.addFilter(lambda record: record.levelno >= logging.ERROR)
        logger.addHandler(stderr_handler)
    else:
        stream_handler = logging.StreamHandler(original_stderr)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)


STARTUP_WARNINGS = []


def log_startup_warning(msg):
    logging.warning(msg)
    STARTUP_WARNINGS.append(msg)


def print_startup_warnings():
    for s in STARTUP_WARNINGS:
        logging.warning(s)
    STARTUP_WARNINGS.clear()
