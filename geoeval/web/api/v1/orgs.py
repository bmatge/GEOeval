"""Identité du principal et organisations."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from geoeval.db.models import ApiToken, Organization
from geoeval.web import audit, hierarchy, tenancy
from geoeval.web.api.deps import Principal, current_token, org_context, require_platform_admin, session_user
from geoeval.web.api.schemas import (
    MeOut,
    OrgCreateIn,
    OrgDetailOut,
    OrgOut,
    OrgPatch,
    OrgRefOut,
    OrgRoleOut,
    TokenRefOut,
)
from geoeval.web.auth import CurrentUser
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


def _detail(db: Session, org: Organization) -> OrgDetailOut:
    out = OrgDetailOut.model_validate(org)
    out.lineage = [OrgRefOut.model_validate(a) for a in hierarchy.lineage(db, org)]
    return out


@router.get("/orgs/{org_slug}", response_model=OrgDetailOut, summary="Une entité, avec ses ancêtres")
def get_org(principal: Principal = Depends(org_context), db: Session = Depends(get_db)):
    return _detail(db, principal.org)


@router.get("/orgs/{org_slug}/children", response_model=list[OrgRefOut], summary="Sous-entités directes")
def get_children(principal: Principal = Depends(org_context), db: Session = Depends(get_db)):
    return [OrgRefOut.model_validate(o) for o in hierarchy.children(db, principal.org.id)]


def _parent_by_slug(db: Session, slug: Optional[str]) -> Optional[Organization]:
    if slug is None:
        return None
    parent = tenancy.get_org_by_slug(db, slug)
    if parent is None:
        raise HTTPException(status_code=404, detail=f"Entité parente {slug!r} introuvable.")
    return parent


@router.post("/orgs", response_model=OrgDetailOut, status_code=status.HTTP_201_CREATED,
             summary="Créer une entité (admin plateforme), à la racine ou sous une entité parente")
def create_org(body: OrgCreateIn, admin: CurrentUser = Depends(require_platform_admin), db: Session = Depends(get_db)):
    parent = _parent_by_slug(db, body.parent_slug)
    try:
        org = tenancy.create_org(db, name=body.name, slug=body.slug, created_by=admin.id,
                                 parent=parent, kind=body.kind, siret=body.siret)
    except hierarchy.HierarchyError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409 if "déjà utilisé" in str(exc) else 400, detail=str(exc))
    audit.record(db, user_id=admin.id, org_id=org.id, action="create", entity_type="organization", entity_id=org.id,
                 meta={"slug": org.slug, "kind": org.kind, "parent_id": org.parent_id, "via": "api"})
    return _detail(db, org)


@router.patch("/orgs/{org_slug}", response_model=OrgDetailOut,
              summary="Qualifier ou rattacher une entité (admin plateforme) — `parent_slug: null` = racine")
def update_org(org_slug: str, body: OrgPatch, admin: CurrentUser = Depends(require_platform_admin), db: Session = Depends(get_db)):
    org = tenancy.get_org_by_slug(db, org_slug)
    if org is None:
        raise HTTPException(status_code=404, detail="Organisation introuvable.")
    fields = body.model_dump(exclude_unset=True)
    old_parent = org.parent_id
    try:
        hierarchy.update_and_move(
            db, org, name=fields.get("name"), kind=fields.get("kind"),
            siret=fields.get("siret"), set_siret="siret" in fields,
            move_to=_parent_by_slug(db, fields.get("parent_slug")), do_move="parent_slug" in fields,
        )
    except hierarchy.HierarchyError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    audit.record(db, user_id=admin.id, org_id=org.id, action="update", entity_type="organization", entity_id=org.id,
                 meta={"fields": sorted(fields), "parent_from": old_parent, "parent_to": org.parent_id, "via": "api"})
    db.refresh(org)
    return _detail(db, org)
