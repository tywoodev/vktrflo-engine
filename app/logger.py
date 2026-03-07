from collections import deque
from datetime import datetime
import io
import logging
import sys
import threading

logs = None
stdout_interceptor = None
stderr_interceptor = None


class LogInterceptor(io.TextIOBase):
    def __init__(self, stream,  *args, **kwargs):
        self._stream = stream
        self._encoding = getattr(stream, "encoding", "utf-8")
        self._errors = getattr(stream, "errors", None)
        self._line_buffering = getattr(stream, "line_buffering", False)
        self._lock = threading.Lock()
        self._flush_callbacks = []
        self._logs_since_flush = []

    def write(self, data):
        entry = {"t": datetime.now().isoformat(), "m": data}
        with self._lock:
            self._logs_since_flush.append(entry)

            # Simple handling for cr to overwrite the last output if it isnt a full line
            # else logs just get full of progress messages
            if (
                isinstance(data, str)
                and data.startswith("\r")
                and logs
                and not logs[-1]["m"].endswith("\n")
            ):
                logs.pop()
            logs.append(entry)
        return self._stream.write(data)

    def flush(self):
        try:
            self._stream.flush()
        except (OSError, ValueError):
            # Windows console wrappers used by ComfyUI-Manager/colorama can
            # sporadically throw Errno 22 on flush. Logging must never break
            # node execution or manager UI flows because a console sink is flaky.
            pass
        for cb in self._flush_callbacks:
            cb(self._logs_since_flush)
        self._logs_since_flush = []

    def on_flush(self, callback):
        self._flush_callbacks.append(callback)

    def fileno(self):
        return self._stream.fileno()

    def isatty(self):
        return self._stream.isatty()

    def readable(self):
        return False

    def writable(self):
        return True

    def seekable(self):
        return False

    @property
    def encoding(self):
        return self._encoding

    @property
    def errors(self):
        return self._errors

    @property
    def line_buffering(self):
        return self._line_buffering

    @property
    def buffer(self):
        return getattr(self._stream, "buffer", None)

    def __getattr__(self, name):
        return getattr(self._stream, name)


def get_logs():
    return logs


def on_flush(callback):
    if stdout_interceptor is not None:
        stdout_interceptor.on_flush(callback)
    if stderr_interceptor is not None:
        stderr_interceptor.on_flush(callback)

def setup_logger(log_level: str = 'INFO', capacity: int = 300, use_stdout: bool = False):
    global logs
    if logs:
        return

    # Override output streams and log to buffer
    logs = deque(maxlen=capacity)

    global stdout_interceptor
    global stderr_interceptor
    stdout_interceptor = sys.stdout = LogInterceptor(sys.stdout)
    stderr_interceptor = sys.stderr = LogInterceptor(sys.stderr)

    # Setup default global logger
    logger = logging.getLogger()
    logger.setLevel(log_level)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter("%(message)s"))

    if use_stdout:
        # Only errors and critical to stderr
        stream_handler.addFilter(lambda record: not record.levelno < logging.ERROR)

        # Lesser to stdout
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setFormatter(logging.Formatter("%(message)s"))
        stdout_handler.addFilter(lambda record: record.levelno < logging.ERROR)
        logger.addHandler(stdout_handler)

    logger.addHandler(stream_handler)


STARTUP_WARNINGS = []


def log_startup_warning(msg):
    logging.warning(msg)
    STARTUP_WARNINGS.append(msg)


def print_startup_warnings():
    for s in STARTUP_WARNINGS:
        logging.warning(s)
    STARTUP_WARNINGS.clear()
