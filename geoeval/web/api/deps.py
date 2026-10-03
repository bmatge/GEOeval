"""
Authentification et autorisation de l'API v1.

Deux identités possibles, dans cet ordre :
1. jeton d'organisation ``Authorization: Bearer geoeval_…`` (clients machine) ;
2. session navigateur (cookie), posée par AuthMiddleware sur request.state.user.

Sans identité, le principal est anonyme : seuls les points d'accès publics en
lecture (ADR-087 : évaluations, statistiques) répondent.

Les règles sont les mêmes que l'UI (geoeval.web.deps) : une organisation
inconnue ou non accessible renvoie 404 pour ne pas divulguer son existence.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, Path, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from geoeval.db.models import ApiToken, Organization
from geoeval.web import api_tokens
from geoeval.web.auth import CurrentUser
from geoeval.web.deps import get_db
from geoeval.web.tenancy import effective_role_for_user, get_org_by_slug, resolve_role, role_at_least

bearer_scheme = HTTPBearer(auto_error=False, description="Jeton d'organisation (geoeval_…)")


@dataclass
class Principal:
    kind: str                      # "token" | "session" | "anonymous"
    org: Organization
    role: Optional[str]            # viewer | editor | org_admin | None
    user_id: Optional[int] = None
    email: Optional[str] = None
    token_id: Optional[int] = None
    is_platform_admin: bool = False

    @property
    def is_member(self) -> bool:
        return self.role is not None

    def audit_meta(self) -> dict:
        meta = {"via": "api", "kind": self.kind}
        if self.token_id is not None:
            meta["token_id"] = self.token_id
        return meta


def _token_from_credentials(
    db: Session, credentials: Optional[HTTPAuthorizationCredentials]
) -> Optional[ApiToken]:
    if credentials is None:
        return None
    if credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Schéma d'authentification non supporté (Bearer attendu).")
    token = api_tokens.authenticate(db, credentials.credentials)
    if token is None:
        raise HTTPException(status_code=401, detail="Jeton d'API invalide, expiré ou révoqué.")
    return token


def current_token(
    db: Session = Depends(get_db),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> Optional[ApiToken]:
    """Jeton porté par la requête (None si absent). 401 si présent mais invalide."""
    return _token_from_credentials(db, credentials)


def session_user(request: Request) -> Optional[CurrentUser]:
    return getattr(request.state, "user", None)


def org_context(
    org_slug: str = Path(...),
    request: Request = None,
    db: Session = Depends(get_db),
    token: Optional[ApiToken] = Depends(current_token),
) -> Principal:
    """Résout l'organisation et le principal (jeton, session ou anonyme)."""
    org = get_org_by_slug(db, org_slug)
    if org is None:
        raise HTTPException(status_code=404, detail="Organisation introuvable.")

    if token is not None:
        # Rôle du jeton hérité vers le bas (arbitrage E2) : valable sur son entité
        # et tout son sous-arbre, jamais au-dessus ni à côté (404 sans divulgation).
        role = resolve_role({token.organization_id: token.role}, org)
        if role is None:
            raise HTTPException(status_code=404, detail="Organisation introuvable.")
        return Principal(kind="token", org=org, role=role, token_id=token.id, user_id=token.created_by)

    user = session_user(request)
    if user is not None:
        role = effective_role_for_user(user, org)
        return Principal(kind="session", org=org, role=role, user_id=user.id, email=user.email,
                         is_platform_admin=user.is_platform_admin)

    return Principal(kind="anonymous", org=org, role=None)


def require_role(min_role: str):
    """Rôle effectif ≥ min_role. Anonyme → 401 ; connecté non membre → 404 ; rôle insuffisant → 403."""

    def _dep(principal: Principal = Depends(org_context)) -> Principal:
        if principal.kind == "anonymous":
            raise HTTPException(status_code=401, detail="Authentification requise (jeton Bearer ou session).")
        if not principal.is_member:
            raise HTTPException(status_code=404, detail="Organisation introuvable.")
        if not role_at_least(principal.role, min_role):
            raise HTTPException(status_code=403, detail=f"Rôle {min_role!r} requis.")
        return principal

    return _dep


def require_platform_admin(
    request: Request,
    token: Optional[ApiToken] = Depends(current_token),
) -> CurrentUser:
    """Administration plateforme : session d'un admin plateforme uniquement.
    Un jeton d'organisation n'est jamais admin plateforme (403)."""
    if token is not None:
        raise HTTPException(status_code=403, detail="Réservé à l'administration plateforme (jeton d'organisation refusé).")
    user = session_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentification requise (session d'administration plateforme).")
    if not user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Réservé à l'administration plateforme.")
    return user


@dataclass
class Actor:
    """Auteur d'une opération de structure (création / rattachement d'entités)."""

    kind: str                              # "token" | "session"
    anchors: dict[int, str]                # {org_id: rôle} sur lesquels il s'appuie
    is_platform_admin: bool = False
    user_id: Optional[int] = None
    token_id: Optional[int] = None

    def audit_meta(self) -> dict:
        meta = {"via": "api", "kind": self.kind}
        if self.token_id is not None:
            meta["token_id"] = self.token_id
        return meta


def structure_actor(
    request: Request,
    token: Optional[ApiToken] = Depends(current_token),
) -> Actor:
    """Jeton (rôle hérité de son entité) ou session ; les droits fins sont vérifiés
    par tenancy.can_create_under / can_qualify / can_restructure. Anonyme → 401."""
    if token is not None:
        return Actor(kind="token", anchors={token.organization_id: token.role}, user_id=token.created_by, token_id=token.id)
    user = session_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentification requise (jeton Bearer ou session).")
    return Actor(kind="session", anchors=dict(user.memberships), is_platform_admin=user.is_platform_admin, user_id=user.id)
