"""
DAO vérité de référence versionnée (ADR-079 §1).

Une seule row active par `test_id` (`valid_to IS NULL`). L'édition crée une
nouvelle version et clôt l'ancienne — pas de perte d'historique. Résolution
temporelle par (test_id, ts).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import TestGroundTruth


def get_active(session: Session, test_id: int) -> Optional[TestGroundTruth]:
    return session.execute(
        select(TestGroundTruth).where(
            TestGroundTruth.test_id == test_id,
            TestGroundTruth.valid_to.is_(None),
        )
    ).scalar_one_or_none()


def get_at(
    session: Session, test_id: int, ts: Optional[datetime] = None
) -> Optional[TestGroundTruth]:
    """Vérité en vigueur pour un test à une date donnée (défaut: now)."""
    when = ts or datetime.now(timezone.utc)
    return session.execute(
        select(TestGroundTruth).where(
            TestGroundTruth.test_id == test_id,
            TestGroundTruth.valid_from <= when,
            (TestGroundTruth.valid_to.is_(None)) | (TestGroundTruth.valid_to > when),
        ).order_by(TestGroundTruth.valid_from.desc())
    ).scalar_one_or_none()


def list_versions(session: Session, test_id: int) -> list[TestGroundTruth]:
    return list(
        session.execute(
            select(TestGroundTruth)
            .where(TestGroundTruth.test_id == test_id)
            .order_by(TestGroundTruth.version.desc())
        ).scalars().all()
    )


def create_new_version(
    session: Session,
    *,
    test_id: int,
    reference_answer: str,
    reference_urls: Optional[list[str]] = None,
    created_by: Optional[int] = None,
    notes: Optional[str] = None,
) -> TestGroundTruth:
    """Crée une nouvelle version + clôt la précédente."""
    now = datetime.now(timezone.utc)
    active = get_active(session, test_id)
    next_version = 1
    if active is not None:
        active.valid_to = now
        next_version = active.version + 1
    row = TestGroundTruth(
        test_id=test_id,
        version=next_version,
        reference_answer=reference_answer.strip(),
        reference_urls=reference_urls or None,
        valid_from=now,
        valid_to=None,
        created_by=created_by,
        notes=notes or None,
    )
    session.add(row)
    session.commit()
    return row
