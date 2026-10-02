"""Questions (tests) d'une organisation — lecture, membres."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from geoeval.web import audit, ground_truth, services
from geoeval.web import perimeters as perimeters_svc
from geoeval.web.api.deps import Principal, require_role
from geoeval.web.api.schemas import (
    GroundTruthIn,
    GroundTruthOut,
    Page,
    QuestionDetailOut,
    QuestionIn,
    QuestionOut,
    QuestionPatch,
    paginate,
)
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


# ---- Écriture (lot 1.3c, editor+) -----------------------------------
# Jamais de DELETE : une question référencée par des évaluations est
# désactivée, pas supprimée (ADR-076). La vérité de référence est versionnée.
def _get_or_404(db: Session, org_id: int, test_id: int):
    t = services.get_test(db, org_id, test_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Question introuvable.")
    return t


@router.post("", response_model=QuestionOut, status_code=status.HTTP_201_CREATED, summary="Créer une question (editor+)")
def create_question(body: QuestionIn, principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    if perimeters_svc.get_by_id(db, principal.org.id, body.perimeter_id) is None:
        raise HTTPException(status_code=400, detail="Périmètre invalide.")
    t = services.create_test(
        db, principal.org.id, perimeter_id=body.perimeter_id, prompt=body.prompt, expected_answer=body.expected_answer,
        response_quality_prompt_id=body.response_quality_prompt_id, citation_quality_prompt_id=body.citation_quality_prompt_id,
    )
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action="create", entity_type="test",
                 entity_id=t.test_id, meta={"perimeter_id": t.perimeter_id, **principal.audit_meta()})
    return _out(t)


@router.patch("/{test_id}", response_model=QuestionOut, summary="Modifier une question (editor+) — champs optionnels, déplacement de périmètre inclus")
def update_question(test_id: int, body: QuestionPatch, principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    t = _get_or_404(db, principal.org.id, test_id)
    fields = body.model_dump(exclude_unset=True)
    if "perimeter_id" in fields and fields["perimeter_id"] != t.perimeter_id:
        try:
            perimeters_svc.move_test(db, org_id=principal.org.id, test_id=test_id, to_perimeter_id=fields["perimeter_id"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    if any(k in fields for k in ("prompt", "expected_answer", "response_quality_prompt_id", "citation_quality_prompt_id")):
        services.update_test(
            db, principal.org.id, test_id,
            prompt=fields.get("prompt", t.prompt),
            expected_answer=fields.get("expected_answer", t.expected_answer),
            response_quality_prompt_id=fields.get("response_quality_prompt_id", t.response_quality_prompt_id),
            citation_quality_prompt_id=fields.get("citation_quality_prompt_id", t.citation_quality_prompt_id),
        )
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action="update", entity_type="test",
                 entity_id=test_id, meta={"fields": sorted(fields), **principal.audit_meta()})
    db.refresh(t)
    return _out(t)


@router.post("/{test_id}/deactivate", response_model=QuestionOut, summary="Désactiver une question (editor+) — l'historique est conservé")
def deactivate_question(test_id: int, principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    t = _get_or_404(db, principal.org.id, test_id)
    services.deactivate_test(db, principal.org.id, test_id)
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action="deactivate", entity_type="test",
                 entity_id=test_id, meta=principal.audit_meta())
    db.refresh(t)
    return _out(t)


@router.post("/{test_id}/reactivate", response_model=QuestionOut, summary="Réactiver une question (editor+)")
def reactivate_question(test_id: int, principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    t = _get_or_404(db, principal.org.id, test_id)
    services.reactivate_test(db, principal.org.id, test_id)
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action="reactivate", entity_type="test",
                 entity_id=test_id, meta=principal.audit_meta())
    db.refresh(t)
    return _out(t)


@router.get("/{test_id}/ground-truth", response_model=list[GroundTruthOut], summary="Versions de la vérité de référence, plus récente d'abord")
def list_ground_truth(test_id: int, principal: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    _get_or_404(db, principal.org.id, test_id)
    return [GroundTruthOut.model_validate(v) for v in ground_truth.list_versions(db, test_id)]


@router.post("/{test_id}/ground-truth", response_model=GroundTruthOut, status_code=status.HTTP_201_CREATED,
             summary="Nouvelle version de la vérité de référence (editor+) — la précédente est clôturée, jamais écrasée")
def create_ground_truth(test_id: int, body: GroundTruthIn, principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    _get_or_404(db, principal.org.id, test_id)
    row = ground_truth.create_new_version(
        db, test_id=test_id, reference_answer=body.reference_answer,
        reference_urls=[u.strip() for u in body.reference_urls if u.strip()] or None,
        created_by=principal.user_id, notes=body.notes,
    )
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action="create", entity_type="test_ground_truth",
                 entity_id=row.id, meta={"test_id": test_id, "version": row.version, **principal.audit_meta()})
    return GroundTruthOut.model_validate(row)
