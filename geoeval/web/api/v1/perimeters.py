"""Périmètres (sites) d'une organisation — lecture, membres."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from geoeval.web import audit
from geoeval.web import perimeters as perimeters_svc
from geoeval.web.api.deps import Principal, require_role
from geoeval.web.api.schemas import PerimeterIn, PerimeterOut, PerimeterPatch
from geoeval.web.deps import get_db

router = APIRouter(prefix="/orgs/{org_slug}/perimeters", tags=["périmètres"])


def _out(db: Session, p) -> PerimeterOut:
    out = PerimeterOut.model_validate(p)
    out.n_questions = perimeters_svc.count_tests(db, p.id)
    return out


@router.get("", response_model=list[PerimeterOut], summary="Périmètres de l'organisation")
def list_perimeters(principal: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    return [_out(db, p) for p in perimeters_svc.list_for_org(db, principal.org.id)]


@router.get("/{perimeter_id}", response_model=PerimeterOut, summary="Un périmètre")
def get_perimeter(perimeter_id: int, principal: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    p = perimeters_svc.get_by_id(db, principal.org.id, perimeter_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Périmètre introuvable.")
    return _out(db, p)


# ---- Écriture (lot 1.3c, editor+) -----------------------------------
def _value_error(exc: ValueError) -> HTTPException:
    msg = str(exc)
    if "introuvable" in msg:
        return HTTPException(status_code=404, detail=msg)
    if "existe déjà" in msg or "contient encore" in msg or "pas supprimable" in msg:
        return HTTPException(status_code=409, detail=msg)
    return HTTPException(status_code=400, detail=msg)


@router.post("", response_model=PerimeterOut, status_code=status.HTTP_201_CREATED, summary="Créer un périmètre (editor+)")
def create_perimeter(body: PerimeterIn, principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    try:
        p = perimeters_svc.create(
            db, org_id=principal.org.id, name=body.name, slug=body.slug, kind=body.kind,
            home_url=body.home_url, description=body.description, created_by=principal.user_id,
        )
    except ValueError as exc:
        raise _value_error(exc)
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action="create", entity_type="perimeter",
                 entity_id=p.id, meta={"slug": p.slug, "name": p.name, **principal.audit_meta()})
    return _out(db, p)


@router.patch("/{perimeter_id}", response_model=PerimeterOut, summary="Modifier un périmètre (editor+) — champs optionnels")
def update_perimeter(perimeter_id: int, body: PerimeterPatch, principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    p = perimeters_svc.get_by_id(db, principal.org.id, perimeter_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Périmètre introuvable.")
    fields = body.model_dump(exclude_unset=True)
    try:
        p = perimeters_svc.update(
            db, org_id=principal.org.id, perimeter_id=perimeter_id,
            name=fields.get("name", p.name), kind=fields.get("kind", p.kind),
            home_url=fields.get("home_url", p.home_url), description=fields.get("description", p.description),
        )
    except ValueError as exc:
        raise _value_error(exc)
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action="update", entity_type="perimeter",
                 entity_id=perimeter_id, meta={"fields": sorted(fields), **principal.audit_meta()})
    return _out(db, p)


@router.delete("/{perimeter_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Supprimer un périmètre vide (editor+) — 409 s'il contient des questions")
def delete_perimeter(perimeter_id: int, principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    try:
        perimeters_svc.delete(db, principal.org.id, perimeter_id)
    except ValueError as exc:
        raise _value_error(exc)
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action="delete", entity_type="perimeter",
                 entity_id=perimeter_id, meta=principal.audit_meta())
    return Response(status_code=status.HTTP_204_NO_CONTENT)
