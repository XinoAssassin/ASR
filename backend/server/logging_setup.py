"""Process logging for the server (``ASR_LOG_LEVEL`` / ``ASR_LOG_FORMAT`` / ``ASR_LOG_FILE``).

One set of handlers on the root logger, shared by ``asr.*``, ``server.*`` and uvicorn (``asr serve``
passes ``log_config=None`` so uvicorn's loggers propagate here). Every handler carries a redaction
filter: URL query strings (signed result URLs), bearer tokens and ``sk-`` keys never reach a log
line (AGENTS.md rule 3).
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from asr.providers.cloud.security import redact

LogFormat = Literal["text", "json"]

TEXT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
MAX_FILE_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5

# Attributes every LogRecord has; anything else was passed via ``extra=`` and goes into JSON.
_STANDARD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "taskName",
}


class SecretRedactingFilter(logging.Filter):
    """Redacts the rendered message (and exception text) of every record passing a handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed record; leave it alone
            return True
        cleaned = redact(message, max_chars=None)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text, max_chars=None)
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, msg, extra fields, exc."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                entry[key] = value if isinstance(value, str | int | float | bool) else str(value)
        if record.exc_text:
            entry["exc"] = record.exc_text
        elif record.exc_info:
            entry["exc"] = redact(self.formatException(record.exc_info), max_chars=None)
        return json.dumps(entry, ensure_ascii=False)


def configure_logging(
    level: str = "INFO", fmt: LogFormat = "text", file: Path | None = None
) -> None:
    """(Re)install the server's handlers on the root logger; idempotent."""
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, "_asr_handler", False)]:
        root.removeHandler(handler)
        handler.close()

    formatter: logging.Formatter = (
        JsonFormatter() if fmt == "json" else logging.Formatter(TEXT_FORMAT)
    )
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if file is not None:
        file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                file, maxBytes=MAX_FILE_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
            )
        )
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(SecretRedactingFilter())
        handler._asr_handler = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel(level.upper())
    # Third-party chatter stays at WARNING unless DEBUG was asked for.
    quiet = logging.DEBUG if level.upper() == "DEBUG" else logging.WARNING
    for name in ("httpx", "httpcore", "modelscope", "urllib3", "multipart"):
        logging.getLogger(name).setLevel(quiet)
