"""Identité du principal et organisations."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from geoeval.db.models import ApiToken, Organization
from geoeval.web import tenancy
from geoeval.web.api.deps import Principal, current_token, org_context, session_user
from geoeval.web.api.schemas import MeOut, OrgOut, OrgRoleOut, TokenRefOut
from geoeval.web.deps import get_db

router = APIRouter(tags=["organisations"])


@router.get("/me", response_model=MeOut, summary="Qui suis-je ? (jeton ou session)")
def me(request: Request, db: Session = Depends(get_db), token: Optional[ApiToken] = Depends(current_token)):
    if token is not None:
        org = db.get(Organization, token.organization_id)
        return MeOut(
            kind="token",
            user_id=token.created_by,
            organizations=[OrgRoleOut(slug=org.slug, role=token.role)] if org else [],
            token=TokenRefOut(id=token.id, name=token.name, prefix=token.prefix, role=token.role),
        )
    user = session_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentification requise (jeton Bearer ou session).")
    return MeOut(
        kind="session",
        user_id=user.id,
        email=user.email,
        is_platform_admin=user.is_platform_admin,
        organizations=[OrgRoleOut(slug=slug, role=role) for slug, (_oid, role) in user.memberships_by_slug.items()],
    )


@router.get("/orgs", response_model=list[OrgOut], summary="Organisations visibles")
def list_orgs(request: Request, db: Session = Depends(get_db), token: Optional[ApiToken] = Depends(current_token)):
    """Jeton : son organisation. Session : ses adhésions (toutes pour un admin
    plateforme). Anonyme : toutes, comme la page d'accueil publique (ADR-087)."""
    if token is not None:
        org = db.get(Organization, token.organization_id)
        return [org] if org else []
    user = session_user(request)
    if user is not None and not user.is_platform_admin:
        return tenancy.list_orgs_for_user(db, user.id)
    return tenancy.list_all_orgs(db)


@router.get("/orgs/{org_slug}", response_model=OrgOut, summary="Une organisation")
def get_org(principal: Principal = Depends(org_context)):
    return principal.org
