"""Questions (tests) d'une organisation — lecture, membres."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from geoeval.web import ground_truth, services
from geoeval.web.api.deps import Principal, require_role
from geoeval.web.api.schemas import GroundTruthOut, Page, QuestionDetailOut, QuestionOut, paginate
from geoeval.web.deps import get_db

router = APIRouter(prefix="/orgs/{org_slug}/questions", tags=["questions"])


def _out(t) -> QuestionOut:
    out = QuestionOut.model_validate(t)
    out.is_active = t.validity_end_at is None
    return out


@router.get("", response_model=Page[QuestionOut], summary="Questions de l'organisation")
def list_questions(
    principal: Principal = Depends(require_role("viewer")),
    db: Session = Depends(get_db),
    perimeter_id: Optional[int] = Query(None),
    active_only: bool = Query(False, description="Ne renvoyer que les questions actives"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    rows = services.list_tests(db, principal.org.id, perimeter_id=perimeter_id)
    if active_only:
        rows = [t for t in rows if t.validity_end_at is None]
    return paginate([_out(t) for t in rows], limit=limit, offset=offset)


@router.get("/{test_id}", response_model=QuestionDetailOut, summary="Une question et sa vérité de référence active")
def get_question(test_id: int, principal: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    t = services.get_test(db, principal.org.id, test_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Question introuvable.")
    out = QuestionDetailOut.model_validate(t)
    out.is_active = t.validity_end_at is None
    gt = ground_truth.get_active(db, test_id)
    out.ground_truth = GroundTruthOut.model_validate(gt) if gt is not None else None
    return out
