"""
Auth déléguée à l'infra VibeLab (ADR-077).

La plateforme (Traefik + gate + Authentik SSO) authentifie l'utilisateur en
amont et injecte trois headers signés :
    X-Gate-Email    (identifiant stable)
    X-Gate-Auth     (« link » ou « oidc », informatif)
    X-Gate-Groups   (CSV — ex. « lab-team » = admin plateforme)

Ce module :
    * relit ces headers à chaque requête ;
    * provisionne à la volée l'utilisateur en base (lazy-provisioning) ;
    * expose la structure `CurrentUser` sur `request.state.user`.

En dev local (`python run_web.py` sans stack gate), la variable
`DEV_FAKE_EMAIL` simule un email connu — pratique pour tester les routes
`/o/{slug}/…` sans monter tout le stack proxy-auth.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from fastapi import Request
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from db import SessionLocal
from models import Membership, User

logger = logging.getLogger("webapp.auth")

PLATFORM_ADMIN_GROUP = "lab-team"


@dataclass
class CurrentUser:
    id: int
    email: str
    groups: list[str] = field(default_factory=list)
    memberships: dict[int, str] = field(default_factory=dict)  # org_id -> role
    memberships_by_slug: dict[str, tuple[int, str]] = field(default_factory=dict)
    is_platform_admin: bool = False

    def role_for_org(self, org_id: int) -> Optional[str]:
        return self.memberships.get(org_id)

    def has_org(self, org_id: int) -> bool:
        return self.is_platform_admin or org_id in self.memberships


def _parse_groups(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    return [g.strip() for g in raw.split(",") if g.strip()]


def _dev_fake_email() -> Optional[str]:
    email = os.environ.get("DEV_FAKE_EMAIL", "").strip()
    return email or None


def _dev_fake_groups() -> list[str]:
    return _parse_groups(os.environ.get("DEV_FAKE_GROUPS", PLATFORM_ADMIN_GROUP))


def load_or_provision_user(session, email: str, groups: list[str]) -> CurrentUser:
    """Charge un `User` existant ou le crée à la volée, puis résout ses memberships."""
    email = email.strip().lower()
    now = datetime.now(timezone.utc)
    user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
    is_platform_admin = PLATFORM_ADMIN_GROUP in groups

    if user is None:
        user = User(
            email=email,
            first_seen_at=now,
            last_seen_at=now,
            is_superuser_cached=is_platform_admin,
        )
        session.add(user)
        session.flush()
    else:
        user.last_seen_at = now
        if user.is_superuser_cached != is_platform_admin:
            user.is_superuser_cached = is_platform_admin

    # Le platform admin voit toutes les orgs mais n'a PAS de row `memberships`
    # implicite : ses accès sont dérivés du groupe `lab-team` du header.
    from models import Organization  # local — évite import circulaire

    rows = session.execute(
        select(Membership.org_id, Membership.role, Organization.slug)
        .join(Organization, Organization.id == Membership.org_id)
        .where(Membership.user_id == user.id)
    ).all()
    memberships = {org_id: role for org_id, role, _slug in rows}
    memberships_by_slug = {slug: (org_id, role) for org_id, role, slug in rows}

    session.commit()

    return CurrentUser(
        id=user.id,
        email=email,
        groups=groups,
        memberships=memberships,
        memberships_by_slug=memberships_by_slug,
        is_platform_admin=is_platform_admin,
    )


class GateAuthMiddleware(BaseHTTPMiddleware):
    """Pose `request.state.user` (CurrentUser | None) pour toutes les requêtes.

    L'absence d'user n'est PAS un rejet ici : les dépendances FastAPI décident
    ensuite quoi faire (les endpoints `/o/{slug}/…` exigent un user, d'autres
    routes peuvent rester publiques — cf. `webapp/deps.py`).
    """

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        header_email = (
            request.headers.get("x-gate-email")
            or request.headers.get("X-Gate-Email")
        )
        if header_email:
            # Vrai header gate : groupes = ceux du header (potentiellement vide).
            email = header_email.strip().lower()
            raw_groups = (
                request.headers.get("x-gate-groups")
                or request.headers.get("X-Gate-Groups")
                or ""
            )
            groups = _parse_groups(raw_groups)
        else:
            # Dev local : DEV_FAKE_EMAIL fournit à la fois l'identité et les groupes.
            fake = _dev_fake_email()
            email = fake.lower() if fake else ""
            groups = _dev_fake_groups() if fake else []

        if email:
            with SessionLocal() as session:
                try:
                    request.state.user = load_or_provision_user(session, email, groups)
                except Exception:  # noqa: BLE001
                    logger.exception("provisioning user %s en échec", email)
                    request.state.user = None
        else:
            request.state.user = None

        return await call_next(request)
