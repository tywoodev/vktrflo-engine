from collections import deque
from datetime import datetime
import logging
import sys
from typing import Callable, Iterable
from app.log_cache import append_entries, get_provider_name

logs = None
_flush_callbacks: list[Callable[[list[dict]], None]] = []


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
    global logs
    if logs is not None:
        return

    logs = deque(maxlen=capacity)
    try:
        provider_name = get_provider_name()
        print(f"[VKTRFLO Log] cache provider: {provider_name}", file=sys.stderr, flush=True)
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
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setFormatter(formatter)
        stdout_handler.addFilter(lambda record: record.levelno < logging.ERROR)
        logger.addHandler(stdout_handler)

        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(formatter)
        stderr_handler.addFilter(lambda record: record.levelno >= logging.ERROR)
        logger.addHandler(stderr_handler)
    else:
        stream_handler = logging.StreamHandler(sys.stderr)
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
