"""
Helper audit trail (ADR-077 §6).

Appelé depuis les handlers d'écriture pour enregistrer une action utilisateur.
Volontairement best-effort : une écriture d'audit qui échoue ne doit jamais
faire échouer l'action métier (on log l'échec côté serveur).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from models import AuditLog

logger = logging.getLogger("webapp.audit")


def record(
    session: Session,
    *,
    user_id: Optional[int],
    org_id: Optional[int],
    action: str,
    entity_type: str,
    entity_id: Optional[int] = None,
    meta: Optional[dict[str, Any]] = None,
) -> None:
    """Écrit une ligne d'audit. Best-effort : rollback silencieux en cas d'échec."""
    try:
        row = AuditLog(
            user_id=user_id,
            org_id=org_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            meta_json=meta or None,
        )
        session.add(row)
        session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("audit_log écriture échouée (action=%s, entity=%s)", action, entity_type)
        session.rollback()
