"""
Auth applicative (ADR-086, amende ADR-077).

Résolution d'identité, dans l'ordre :
    1. session applicative (cookie signé, posé par /login ou le callback OIDC) ;
    2. headers proxy-auth `X-Gate-Email` / `X-Gate-Groups` si AUTH_PROXY_ENABLED=1
       (réversibilité ADR-060 — redeployer derrière le gate = flipper une env) ;
    3. DEV_FAKE_EMAIL en dev local (jamais en prod).

Le rôle plateforme vit en base (`users.is_platform_admin`), bootstrapé par
GEOEVAL_ADMIN_EMAILS (CSV) — plus dérivé du groupe `lab-team`, sauf en mode
proxy où le comportement ADR-077 est conservé.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import bcrypt
from fastapi import Request
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from db import SessionLocal
from models import Membership, Organization, User

logger = logging.getLogger("webapp.auth")

PLATFORM_ADMIN_GROUP = "lab-team"
SESSION_USER_KEY = "uid"


@dataclass
class CurrentUser:
    id: int
    email: str
    groups: list[str] = field(default_factory=list)
    memberships: dict[int, str] = field(default_factory=dict)  # org_id -> role
    memberships_by_slug: dict[str, tuple[int, str]] = field(default_factory=dict)
    is_platform_admin: bool = False
    auth_provider: str = "local"

    def role_for_org(self, org_id: int) -> Optional[str]:
        return self.memberships.get(org_id)

    def has_org(self, org_id: int) -> bool:
        return self.is_platform_admin or org_id in self.memberships


# ---------------------------------------------------------------------
# Helpers env
# ---------------------------------------------------------------------
def _parse_groups(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    return [g.strip() for g in raw.split(",") if g.strip()]


def _dev_fake_email() -> Optional[str]:
    email = os.environ.get("DEV_FAKE_EMAIL", "").strip()
    return email or None


def _dev_fake_groups() -> list[str]:
    return _parse_groups(os.environ.get("DEV_FAKE_GROUPS", PLATFORM_ADMIN_GROUP))


def proxy_auth_enabled() -> bool:
    return os.environ.get("AUTH_PROXY_ENABLED", "0").strip() in ("1", "true", "yes")


def bootstrap_admin_emails() -> set[str]:
    raw = os.environ.get("GEOEVAL_ADMIN_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


# ---------------------------------------------------------------------
# Mots de passe (bcrypt)
# ---------------------------------------------------------------------
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: Optional[str]) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except ValueError:
        return False


# ---------------------------------------------------------------------
# Chargement / provisioning
# ---------------------------------------------------------------------
def _resolve_memberships(session, user_id: int) -> tuple[dict, dict]:
    rows = session.execute(
        select(Membership.org_id, Membership.role, Organization.slug)
        .join(Organization, Organization.id == Membership.org_id)
        .where(Membership.user_id == user_id)
    ).all()
    memberships = {org_id: role for org_id, role, _slug in rows}
    memberships_by_slug = {slug: (org_id, role) for org_id, role, slug in rows}
    return memberships, memberships_by_slug


def _maybe_bootstrap_admin(user: User) -> None:
    """Promeut en admin plateforme les emails listés dans GEOEVAL_ADMIN_EMAILS.

    Promotion uniquement (jamais de rétrogradation automatique) : la liste env
    est un bootstrap, la gestion courante passe par /admin/users.
    """
    if not user.is_platform_admin and user.email in bootstrap_admin_emails():
        user.is_platform_admin = True


def _to_current_user(session, user: User, groups: list[str]) -> CurrentUser:
    memberships, memberships_by_slug = _resolve_memberships(session, user.id)
    return CurrentUser(
        id=user.id,
        email=user.email,
        groups=groups,
        memberships=memberships,
        memberships_by_slug=memberships_by_slug,
        is_platform_admin=user.is_platform_admin,
        auth_provider=user.auth_provider,
    )


def load_user_by_id(session, user_id: int) -> Optional[CurrentUser]:
    """Identité depuis la session applicative (cookie signé)."""
    user = session.get(User, user_id)
    if user is None:
        return None
    user.last_seen_at = datetime.now(timezone.utc)
    _maybe_bootstrap_admin(user)
    cu = _to_current_user(session, user, groups=[])
    session.commit()
    return cu


def load_or_provision_user(session, email: str, groups: list[str]) -> CurrentUser:
    """Charge un `User` existant ou le crée à la volée (modes proxy/dev + admin).

    En mode proxy, le groupe `lab-team` vaut promotion admin plateforme
    (comportement ADR-077 conservé) — la promotion est persistée en base.
    """
    email = email.strip().lower()
    now = datetime.now(timezone.utc)
    user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()

    if user is None:
        user = User(email=email, first_seen_at=now, last_seen_at=now)
        session.add(user)
        session.flush()
    else:
        user.last_seen_at = now

    if PLATFORM_ADMIN_GROUP in groups and not user.is_platform_admin:
        user.is_platform_admin = True
    _maybe_bootstrap_admin(user)

    cu = _to_current_user(session, user, groups=groups)
    session.commit()
    return cu


class AuthMiddleware(BaseHTTPMiddleware):
    """Pose `request.state.user` (CurrentUser | None) pour toutes les requêtes.

    L'absence d'user n'est PAS un rejet ici : les dépendances FastAPI décident
    ensuite quoi faire (cf. `webapp/deps.py`).
    """

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        request.state.user = None

        # 1. Session applicative (login local ou OIDC).
        uid = request.session.get(SESSION_USER_KEY)
        if uid is not None:
            with SessionLocal() as session:
                try:
                    request.state.user = load_user_by_id(session, int(uid))
                except Exception:  # noqa: BLE001
                    logger.exception("chargement session uid=%s en échec", uid)
            if request.state.user is None:
                # User supprimé ou session corrompue : purge du cookie.
                request.session.pop(SESSION_USER_KEY, None)

        # 2. Headers proxy-auth (opt-in, réversibilité ADR-060/077).
        if request.state.user is None and proxy_auth_enabled():
            header_email = request.headers.get("x-gate-email")
            if header_email:
                groups = _parse_groups(request.headers.get("x-gate-groups", ""))
                with SessionLocal() as session:
                    try:
                        request.state.user = load_or_provision_user(
                            session, header_email, groups
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception("provisioning proxy %s en échec", header_email)

        # 3. Dev local sans stack (jamais actif si un user est déjà résolu).
        if request.state.user is None:
            fake = _dev_fake_email()
            if fake:
                with SessionLocal() as session:
                    try:
                        request.state.user = load_or_provision_user(
                            session, fake, _dev_fake_groups()
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception("provisioning DEV_FAKE_EMAIL en échec")

        return await call_next(request)
