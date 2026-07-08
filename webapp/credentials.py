"""
DAO org_credentials (ADR-078 §2) — BYOK par org sur un modèle du catalogue.

Séparé de services.py pour cloisonner la logique crypto : ce module est le
seul endroit où on manipule des blobs Fernet en lecture / écriture.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import OrgCredential
from webapp.crypto import encrypt_secret


def list_for_org(session: Session, org_id: int) -> list[OrgCredential]:
    return list(
        session.execute(
            select(OrgCredential)
            .where(OrgCredential.organization_id == org_id)
            .order_by(OrgCredential.model_id)
        ).scalars().all()
    )


def get_for_model(
    session: Session, org_id: int, model_id: int
) -> Optional[OrgCredential]:
    return session.execute(
        select(OrgCredential).where(
            OrgCredential.organization_id == org_id,
            OrgCredential.model_id == model_id,
        )
    ).scalar_one_or_none()


def upsert(
    session: Session,
    *,
    org_id: int,
    model_id: int,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,      # None = inchangée si row existante
    extra_headers: Optional[dict[str, Any]] = None,
    is_active: bool = True,
    clear_api_key: bool = False,        # True force la mise à NULL
) -> OrgCredential:
    """Crée ou met à jour la clé BYOK. Si `api_key` est None et la row existe,
    la clé est conservée (chiffrée en base). `clear_api_key=True` la RESET."""
    row = get_for_model(session, org_id, model_id)
    if row is None:
        row = OrgCredential(
            organization_id=org_id,
            model_id=model_id,
            base_url=base_url or None,
            api_key_encrypted=encrypt_secret(api_key) if api_key else None,
            extra_headers=extra_headers or None,
            is_active=is_active,
        )
        session.add(row)
    else:
        row.base_url = base_url or None
        row.extra_headers = extra_headers or None
        row.is_active = is_active
        if clear_api_key:
            row.api_key_encrypted = None
        elif api_key:
            row.api_key_encrypted = encrypt_secret(api_key)
    session.commit()
    return row


def delete(session: Session, org_id: int, model_id: int) -> None:
    row = get_for_model(session, org_id, model_id)
    if row is not None:
        session.delete(row)
        session.commit()
