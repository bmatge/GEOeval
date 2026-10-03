"""Questions (tests) d'une organisation — lecture, membres."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from geoeval.web import audit, ground_truth, services, themes
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


def _out(t, theme_ids: Optional[list[int]] = None) -> QuestionOut:
    out = QuestionOut.model_validate(t)
    out.is_active = t.status == "published"
    out.theme_ids = theme_ids or []
    return out


def _theme_ids(db: Session, test_id: int) -> list[int]:
    return [th.id for th in themes.for_tests(db, [test_id]).get(test_id, [])]


def _valid_theme_ids(db: Session, ids: list[int]) -> list[int]:
    try:
        return themes.validate_ids(db, ids)
    except themes.ThemeError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)


@router.get("", response_model=Page[QuestionOut], summary="Questions de l'organisation")
def list_questions(
    principal: Principal = Depends(require_role("viewer")),
    db: Session = Depends(get_db),
    perimeter_id: Optional[int] = Query(None),
    theme_id: Optional[int] = Query(None, description="Ne renvoyer que les questions portant ce thème"),
    active_only: bool = Query(False, description="Ne renvoyer que les questions publiées"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    rows = services.list_tests(db, principal.org.id, perimeter_id=perimeter_id)
    if active_only:
        rows = [t for t in rows if t.status == "published"]
    if theme_id is not None:
        keep = themes.test_ids_with_theme(db, theme_id)
        rows = [t for t in rows if t.test_id in keep]
    tmap = themes.for_tests(db, [t.test_id for t in rows])
    return paginate([_out(t, [th.id for th in tmap.get(t.test_id, [])]) for t in rows], limit=limit, offset=offset)


@router.get("/{test_id}", response_model=QuestionDetailOut, summary="Une question et sa vérité de référence active")
def get_question(test_id: int, principal: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    t = services.get_test(db, principal.org.id, test_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Question introuvable.")
    out = QuestionDetailOut.model_validate(t)
    out.is_active = t.status == "published"
    out.theme_ids = _theme_ids(db, test_id)
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
    theme_ids = _valid_theme_ids(db, body.theme_ids)
    t = services.create_test(
        db, principal.org.id, perimeter_id=body.perimeter_id, prompt=body.prompt, expected_answer=body.expected_answer,
        response_quality_prompt_id=body.response_quality_prompt_id, citation_quality_prompt_id=body.citation_quality_prompt_id,
        status=body.status,
    )
    themes.set_for_test(db, t.test_id, theme_ids)
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action="create", entity_type="test",
                 entity_id=t.test_id, meta={"perimeter_id": t.perimeter_id, **principal.audit_meta()})
    return _out(t, theme_ids)


@router.patch("/{test_id}", response_model=QuestionOut, summary="Modifier une question (editor+) — champs optionnels, déplacement de périmètre inclus")
def update_question(test_id: int, body: QuestionPatch, principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    t = _get_or_404(db, principal.org.id, test_id)
    fields = body.model_dump(exclude_unset=True)
    theme_ids = _valid_theme_ids(db, fields["theme_ids"] or []) if "theme_ids" in fields else None
    if "perimeter_id" in fields and fields["perimeter_id"] != t.perimeter_id:
        try:
            perimeters_svc.move_test(db, org_id=principal.org.id, test_id=test_id, to_perimeter_id=fields["perimeter_id"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    if any(k in fields for k in ("prompt", "expected_answer", "response_quality_prompt_id", "citation_quality_prompt_id")):
        try:
            services.update_test(
                db, principal.org.id, test_id,
                prompt=fields.get("prompt", t.prompt),
                expected_answer=fields.get("expected_answer", t.expected_answer),
                response_quality_prompt_id=fields.get("response_quality_prompt_id", t.response_quality_prompt_id),
                citation_quality_prompt_id=fields.get("citation_quality_prompt_id", t.citation_quality_prompt_id),
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
    if theme_ids is not None:
        themes.set_for_test(db, test_id, theme_ids)
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action="update", entity_type="test",
                 entity_id=test_id, meta={"fields": sorted(fields), **principal.audit_meta()})
    db.refresh(t)
    return _out(t, _theme_ids(db, test_id))


@router.post("/{test_id}/deactivate", response_model=QuestionOut, summary="Désactiver une question (editor+) — l'historique est conservé")
def deactivate_question(test_id: int, principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    t = _get_or_404(db, principal.org.id, test_id)
    services.deactivate_test(db, principal.org.id, test_id)
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action="deactivate", entity_type="test",
                 entity_id=test_id, meta=principal.audit_meta())
    db.refresh(t)
    return _out(t, _theme_ids(db, test_id))


@router.post("/{test_id}/publish", response_model=QuestionOut, summary="Publier un brouillon (editor+)")
def publish_question(test_id: int, principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    t = _get_or_404(db, principal.org.id, test_id)
    try:
        services.publish_test(db, principal.org.id, test_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action="publish", entity_type="test",
                 entity_id=test_id, meta=principal.audit_meta())
    db.refresh(t)
    return _out(t, _theme_ids(db, test_id))


@router.post("/{test_id}/reactivate", response_model=QuestionOut, summary="Réactiver une question (editor+)")
def reactivate_question(test_id: int, principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    t = _get_or_404(db, principal.org.id, test_id)
    try:
        services.reactivate_test(db, principal.org.id, test_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action="reactivate", entity_type="test",
                 entity_id=test_id, meta=principal.audit_meta())
    db.refresh(t)
    return _out(t, _theme_ids(db, test_id))


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
