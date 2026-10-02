"""Périmètres (sites) d'une organisation — lecture, membres."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from geoeval.web import perimeters as perimeters_svc
from geoeval.web.api.deps import Principal, require_role
from geoeval.web.api.schemas import PerimeterOut
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
