"""JSON logs with a per-request correlation id and secret redaction."""

import contextvars
import json
import logging
import re

from backend import config

correlation_id = contextvars.ContextVar("correlation_id", default=None)

# Belt and braces: the code never logs keys or tokens, this catches accidents.
SECRET = re.compile(
    r"(sk-ant-[\w-]{6,}|sk-[\w-]{6,}|AIza[\w-]{20,}|AQ\.[\w.-]{20,}|Bearer\s+["
    r"\w.~+/=-]+|eyJ[\w-]+\.[\w-]+\.[\w-]*)"
)


def redact(s):
    return SECRET.sub("[REDACTED]", s)


class JsonFormatter(logging.Formatter):
    def format(self, record):
        out = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
            "correlationId": correlation_id.get(),
        }
        out.update(getattr(record, "fields", {}))
        if record.exc_info:
            out["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(out, default=str)


def setup():
    h = logging.StreamHandler()
    h.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(config.LOG_LEVEL)
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.access").disabled = (
        True  # replaced by our access log middleware
    )
    logging.getLogger("httpx").setLevel(
        logging.WARNING
    )  # httpx INFO lines carry full URLs
