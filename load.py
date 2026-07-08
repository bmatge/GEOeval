from __future__ import annotations
from sqlalchemy import select
from sqlalchemy.orm import Session
from models import Test


from typing import Iterable, Optional


def load_tests(
    session: Session,
    test_ids: Iterable[int] | None = None,
    active_only: bool = True,
    ready_only: bool = True,
    limit: int | None = None,
    organization_id: Optional[int] = None,
) -> list[Test]:
    """Charge les tests filtrables par ids, activité, préparation et org.

    `organization_id=None` → pas de filtre (pratique pour les CLI et scripts
    de maintenance qui manipulent le corpus historique). L'UI et le job de
    fond DOIVENT toujours passer un `organization_id`.
    """
    stmt = select(Test).order_by(Test.test_id.asc())
    if organization_id is not None:
        stmt = stmt.where(Test.organization_id == organization_id)
    if test_ids:
        stmt = stmt.where(Test.test_id.in_(list(test_ids)))
    if active_only:
        stmt = stmt.where(Test.validity_end_at.is_(None))
    if ready_only:
        stmt = stmt.where(Test.expected_answer.is_not(None))
    if limit is not None:
        stmt = stmt.limit(limit)
    return session.execute(stmt).scalars().all()
