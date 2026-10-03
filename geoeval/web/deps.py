"""
Dépendances FastAPI pour tenancy + RBAC.

Utilisées par les routes préfixées `/o/{org_slug}/…`. Le middleware
`AuthMiddleware` a déjà posé `request.state.user` (CurrentUser | None).

Erreurs volontairement discrètes :
    * pas d'user      → 401
    * org inconnue    → 404 (non-divulgation d'existence)
    * user pas membre → 404 (idem)
    * rôle insuffisant → 403

Rôles hérités (ADR-089 §2.4, E2) : le rôle effectif sur une entité est le
maximum des rôles posés sur elle et ses ancêtres (tenancy.resolve_role). Le
rôle renvoyé est un `EffectiveRole` (chaîne) qui connaît son entité d'ancrage.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, Path, Request
from sqlalchemy.orm import Session

from geoeval.db.session import SessionLocal
from geoeval.web.auth import CurrentUser
from geoeval.web.tenancy import effective_role_for_user, get_org_by_slug, role_at_least


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_user(request: Request) -> CurrentUser:
    user: Optional[CurrentUser] = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Authentification requise (connecte-toi via /login).",
        )
    return user


def require_platform_admin(user: CurrentUser = Depends(require_user)) -> CurrentUser:
    if not user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Accès réservé à l'administration plateforme.")
    return user


def require_org(
    org_slug: str = Path(...),
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Résout org + rôle du user courant. Renvoie (org, role_effectif)."""
    org = get_org_by_slug(db, org_slug)
    if org is None:
        # 404 plutôt que 403 : ne pas divulguer l'existence de l'org.
        raise HTTPException(status_code=404, detail="Organisation introuvable.")
    role = effective_role_for_user(user, org)
    if role is None:
        raise HTTPException(status_code=404, detail="Organisation introuvable.")
    return org, role


def public_org(
    org_slug: str = Path(...),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """Résout l'org SANS exiger de connexion (pages publiques en lecture — ADR-087).

    Renvoie (org, role) : role est celui du user connecté s'il y en a un
    (viewer/editor/org_admin, org_admin implicite pour un platform_admin),
    None pour un visiteur anonyme. 404 si l'org n'existe pas.
    """
    org = get_org_by_slug(db, org_slug)
    if org is None:
        raise HTTPException(status_code=404, detail="Organisation introuvable.")
    user: Optional[CurrentUser] = getattr(request.state, "user", None)
    role = None
    if user is not None:
        role = effective_role_for_user(user, org)
    return org, role


def require_role(min_role: str):
    """Vérifie que le rôle effectif dans l'org est ≥ min_role."""

    def _dep(ctx=Depends(require_org)):
        org, role = ctx
        if not role_at_least(role, min_role):
            raise HTTPException(status_code=403, detail=f"Rôle {min_role!r} requis.")
        return org, role

    return _dep
