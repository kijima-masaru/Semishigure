"""In-memory ring buffer of the application's own log, shown on the デバッグ tab."""

from __future__ import annotations

import logging
import threading
from collections import deque

_FORMAT = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")


class RingHandler(logging.Handler):
    """Keeps the last ``capacity`` formatted log lines (every level)."""

    def __init__(self, capacity: int = 3000):
        super().__init__(level=logging.DEBUG)
        self.lines: deque[str] = deque(maxlen=capacity)
        self._guard = threading.Lock()
        self.setFormatter(_FORMAT)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:  # noqa: BLE001
            self.handleError(record)
            return
        with self._guard:
            self.lines.append(line)

    def tail(self, n: int = 500) -> list[str]:
        with self._guard:
            return list(self.lines)[-n:]


_handler: RingHandler | None = None


def install() -> RingHandler:
    """Attach the ring buffer to the root logger once and make sure INFO from this app reaches it."""
    global _handler
    if _handler is None:
        _handler = RingHandler()
        logging.getLogger().addHandler(_handler)
        app_logger = logging.getLogger("semishigure")
        if app_logger.getEffectiveLevel() > logging.INFO:
            app_logger.setLevel(logging.INFO)
    return _handler
