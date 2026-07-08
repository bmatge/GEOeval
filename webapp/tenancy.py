"""
Résolution d'organisation par slug + helpers de convenance.

Séparé de `services.py` pour ne pas mélanger la couche « auth/tenancy » (qui
lit des IDs) avec les DAO du domaine benchmark (qui filtrent par org_id).
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Invitation, Membership, Organization, User


# Rôles reconnus au sein d'une organisation (voir ADR-077 §3).
ROLES = ("org_admin", "editor", "viewer")

# Poids par rôle pour la comparaison "au moins ce niveau".
_ROLE_WEIGHT = {"viewer": 0, "editor": 1, "org_admin": 2}

INVITATION_TTL = timedelta(days=30)


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
    if not re.fullmatch(r"[a-z0-9][a-z0-9\-]{1,63}", slug):
        raise ValueError(
            f"slug invalide {slug!r}: minuscules, chiffres et tirets uniquement (2–64 chars)."
        )
    org = Organization(name=name.strip(), slug=slug.strip(), created_by=created_by)
    session.add(org)
    session.commit()
    return org


def rename_org(session: Session, org_id: int, *, name: str) -> Organization:
    org = session.get(Organization, org_id)
    if org is None:
        raise ValueError(f"organization {org_id} introuvable")
    org.name = name.strip()
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


def set_membership_role(
    session: Session, *, user_id: int, org_id: int, role: str
) -> Membership:
    if role not in ROLES:
        raise ValueError(f"role invalide: {role!r}")
    m = session.get(Membership, (user_id, org_id))
    if m is None:
        raise ValueError(f"pas de membership user={user_id} / org={org_id}")
    m.role = role
    session.commit()
    return m


def remove_membership(session: Session, *, user_id: int, org_id: int) -> None:
    m = session.get(Membership, (user_id, org_id))
    if m is not None:
        session.delete(m)
        session.commit()


def list_members(session: Session, org_id: int) -> list[dict[str, Any]]:
    """Renvoie les membres d'une org avec leur email et rôle."""
    rows = session.execute(
        select(User.id, User.email, Membership.role, Membership.created_at)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.org_id == org_id)
        .order_by(User.email)
    ).all()
    return [
        dict(user_id=r.id, email=r.email, role=r.role, created_at=r.created_at)
        for r in rows
    ]


def role_at_least(actual: Optional[str], required: str) -> bool:
    if actual is None:
        return False
    return _ROLE_WEIGHT.get(actual, -1) >= _ROLE_WEIGHT[required]


# =====================================================================
# Invitations (ADR-077 §5)
# =====================================================================
def _new_token() -> str:
    return secrets.token_urlsafe(32)


def create_invitation(
    session: Session,
    *,
    org_id: int,
    email: str,
    role: str,
    invited_by: int,
) -> Invitation:
    if role not in ROLES:
        raise ValueError(f"role invalide: {role!r}")
    inv = Invitation(
        org_id=org_id,
        email=email.strip().lower(),
        role=role,
        invited_by=invited_by,
        token=_new_token(),
    )
    session.add(inv)
    session.commit()
    return inv


def list_invitations(session: Session, org_id: int) -> list[Invitation]:
    return list(
        session.execute(
            select(Invitation)
            .where(Invitation.org_id == org_id)
            .order_by(Invitation.created_at.desc())
        ).scalars().all()
    )


def get_invitation_by_token(session: Session, token: str) -> Optional[Invitation]:
    return session.execute(
        select(Invitation).where(Invitation.token == token)
    ).scalar_one_or_none()


def revoke_invitation(session: Session, org_id: int, inv_id: int) -> None:
    inv = session.get(Invitation, inv_id)
    if inv is None or inv.org_id != org_id:
        raise ValueError(f"invitation {inv_id} introuvable pour org {org_id}")
    if inv.accepted_at is not None:
        raise ValueError("invitation déjà acceptée")
    session.delete(inv)
    session.commit()


def invitation_is_expired(inv: Invitation) -> bool:
    if inv.accepted_at is not None:
        return True
    now = datetime.now(timezone.utc)
    return (now - inv.created_at) > INVITATION_TTL


def accept_invitation(
    session: Session, *, token: str, current_email: str
) -> Membership:
    """Vérifie que l'user courant match l'email invité, puis crée le membership.

    Renvoie le membership créé. Lève ValueError si le token est invalide, expiré,
    déjà utilisé, ou si l'email ne correspond pas.
    """
    inv = get_invitation_by_token(session, token)
    if inv is None:
        raise ValueError("token d'invitation invalide")
    if invitation_is_expired(inv):
        raise ValueError("invitation expirée ou déjà acceptée")
    if inv.email.strip().lower() != current_email.strip().lower():
        raise ValueError("cette invitation ne correspond pas à ton adresse email")

    user = session.execute(
        select(User).where(User.email == current_email.strip().lower())
    ).scalar_one()

    existing = session.get(Membership, (user.id, inv.org_id))
    if existing is None:
        m = Membership(user_id=user.id, org_id=inv.org_id, role=inv.role)
        session.add(m)
    else:
        if _ROLE_WEIGHT[inv.role] > _ROLE_WEIGHT[existing.role]:
            existing.role = inv.role
        m = existing

    inv.accepted_at = datetime.now(timezone.utc)
    session.commit()
    return m
