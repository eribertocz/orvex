import json
import logging

from orvex.server.logs import JsonFormatter


def test_json_line_with_extra_fields_and_without_terminal_colors() -> None:
    record = logging.makeLogRecord(
        {"name": "x", "levelname": "INFO", "msg": "request", "request_id": "r-1"}
    )
    record.__dict__["color_message"] = "\x1b[36mrequest\x1b[0m"
    line = json.loads(JsonFormatter().format(record))
    assert line["message"] == "request"
    assert line["request_id"] == "r-1"
    assert "color_message" not in line
