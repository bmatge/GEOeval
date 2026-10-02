"""
Journalisation structurée sur stdout.

Format JSON (une ligne par événement) quand GEOEVAL_LOG_FORMAT=json, ou par
défaut quand la sortie n'est pas un terminal (conteneur) ; texte lisible sinon.
Les champs request_id / job_id / org_id viennent des variables de contexte.
Les loggers uvicorn sont rabattus sur le root pour un format unique ; le
journal d'accès uvicorn est remplacé par celui du middleware.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import IO, Optional

from geoeval.observability.context import current_context

_STANDARD_ATTRS = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}
_configured_for: Optional[str] = None


def log_format() -> str:
    fmt = os.environ.get("GEOEVAL_LOG_FORMAT", "").strip().lower()
    if fmt in ("json", "text"):
        return fmt
    return "text" if sys.stdout.isatty() else "json"


class ContextFilter(logging.Filter):
    """Injecte service + contexte courant dans chaque record."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def filter(self, record: logging.LogRecord) -> bool:
        record.service = self.service
        for k, v in current_context().items():
            setattr(record, k, v)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_") or value is None:
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        ctx = current_context()
        if ctx:
            base += " | " + " ".join(f"{k}={v}" for k, v in ctx.items())
        return base


def configure_logging(service: str, *, stream: Optional[IO[str]] = None, force: bool = False) -> None:
    """Configure le root logger (idempotent par service)."""
    global _configured_for
    if _configured_for == service and not force:
        return
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter() if log_format() == "json" else TextFormatter())
    handler.addFilter(ContextFilter(service))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(os.environ.get("GEOEVAL_LOG_LEVEL", "INFO").upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    # Le journal d'accès uvicorn est remplacé par le middleware (request_id, durée).
    logging.getLogger("uvicorn.access").disabled = True
    for name in ("httpx", "httpx2", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    _configured_for = service
