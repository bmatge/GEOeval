"""Sondes : vivacité (processus) et disponibilité (base + schéma)."""
from __future__ import annotations

from typing import Any

from sqlalchemy import text


def check_db() -> tuple[bool, dict[str, Any]]:
    """Base joignable et schéma présent (table jobs, créée par les migrations)."""
    from geoeval.db.session import SessionLocal

    try:
        with SessionLocal() as session:
            session.execute(text("SELECT 1"))
            session.execute(text("SELECT 1 FROM jobs LIMIT 0"))
        return True, {"database": "ok"}
    except Exception as exc:  # noqa: BLE001
        return False, {"database": "unavailable", "error": type(exc).__name__}
