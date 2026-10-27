"""JSON-lines logging through the standard library (ADR-0021)."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import override

# Attributes every LogRecord has; anything else was passed through `extra=` and is logged.
# `color_message` is Uvicorn's terminal-colored duplicate of the message.
_STANDARD = set(vars(logging.makeLogRecord({}))) | {
    "message",
    "asctime",
    "taskName",
    "color_message",
}


class JsonFormatter(logging.Formatter):
    @override
    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        line |= {key: value for key, value in vars(record).items() if key not in _STANDARD}
        if record.exc_info:
            line["exception"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
