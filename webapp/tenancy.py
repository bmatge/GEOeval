"""
Résolution d'organisation par slug + helpers de convenance.

Séparé de `services.py` pour ne pas mélanger la couche « auth/tenancy » (qui
lit des IDs) avec les DAO du domaine benchmark (qui filtrent par org_id).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Membership, Organization


def get_org_by_slug(session: Session, slug: str) -> Optional[Organization]:
    return session.execute(
        select(Organization).where(Organization.slug == slug)
    ).scalar_one_or_none()


def get_org(session: Session, org_id: int) -> Optional[Organization]:
    return session.get(Organization, org_id)


def list_orgs_for_user(session: Session, user_id: int) -> list[Organization]:
    rows = session.execute(
        select(Organization)
        .join(Membership, Membership.org_id == Organization.id)
        .where(Membership.user_id == user_id)
        .order_by(Organization.name)
    ).scalars().all()
    return list(rows)


def list_all_orgs(session: Session) -> list[Organization]:
    return list(
        session.execute(select(Organization).order_by(Organization.name)).scalars().all()
    )


def create_org(
    session: Session,
    *,
    name: str,
    slug: str,
    created_by: Optional[int] = None,
) -> Organization:
    org = Organization(name=name.strip(), slug=slug.strip(), created_by=created_by)
    session.add(org)
    session.commit()
    return org


def add_membership(
    session: Session,
    *,
    user_id: int,
    org_id: int,
    role: str,
) -> Membership:
    if role not in ROLES:
        raise ValueError(f"role invalide: {role!r} (attendu: {', '.join(ROLES)})")
    m = Membership(user_id=user_id, org_id=org_id, role=role)
    session.add(m)
    session.commit()
    return m


# Rôles reconnus au sein d'une organisation (voir ADR-077 §3).
ROLES = ("org_admin", "editor", "viewer")

# Poids par rôle pour la comparaison "au moins ce niveau".
_ROLE_WEIGHT = {"viewer": 0, "editor": 1, "org_admin": 2}


def role_at_least(actual: Optional[str], required: str) -> bool:
    if actual is None:
        return False
    return _ROLE_WEIGHT.get(actual, -1) >= _ROLE_WEIGHT[required]
