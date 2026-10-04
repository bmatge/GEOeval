"""Validation métier des questions (fin de E8, ADR-089 §2.9).

Relectures en attente : viewer+. Approuver / renvoyer : décision personnelle d'un
validateur, en session (un jeton d'organisation n'a pas d'identité : 403), jamais par
l'auteur de la soumission. Réglage et validateurs : lecture editor+, écriture org_admin.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from geoeval.web import audit, reviews, services, tenancy
from geoeval.web.api.deps import Principal, require_role
from geoeval.web.api.schemas import QuestionOut, ReviewRejectIn, ReviewSettingsIO
from geoeval.web.api.v1.questions import _out, _theme_ids
from geoeval.web.deps import get_db

router = APIRouter(prefix="/orgs/{org_slug}", tags=["validation métier"])


def _http(exc: reviews.ReviewError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


@router.get("/reviews", response_model=list[QuestionOut], summary="Questions en relecture (plus anciennes d'abord)")
def list_pending(principal: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    return [_out(t, _theme_ids(db, t.test_id)) for t in reviews.pending(db, principal.org)]


def _decide(db: Session, principal: Principal, test_id: int, approve: bool, comment: str = "") -> QuestionOut:
    if principal.kind != "session":
        raise HTTPException(status_code=403, detail="Décision personnelle : session requise (jeton d'organisation refusé).")
    t = services.get_test(db, principal.org.id, test_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Question introuvable.")
    try:
        if approve:
            reviews.approve(db, principal.org, t, principal.user_id, is_platform_admin=principal.is_platform_admin)
        else:
            reviews.reject(db, principal.org, t, principal.user_id, comment, is_platform_admin=principal.is_platform_admin)
    except reviews.ReviewError as e:
        raise _http(e)
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action="approve" if approve else "reject",
                 entity_type="test", entity_id=test_id,
                 meta={**({} if approve else {"comment": t.review_comment}), **principal.audit_meta()})
    return _out(t, _theme_ids(db, test_id))


@router.post("/questions/{test_id}/approve", response_model=QuestionOut,
             summary="Approuver une question en relecture (validateur, hors auteur)")
def approve_question(test_id: int, principal: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    return _decide(db, principal, test_id, True)


@router.post("/questions/{test_id}/reject", response_model=QuestionOut,
             summary="Renvoyer une question en brouillon avec un motif (validateur, hors auteur)")
def reject_question(test_id: int, body: ReviewRejectIn, principal: Principal = Depends(require_role("viewer")),
                    db: Session = Depends(get_db)):
    return _decide(db, principal, test_id, False, body.comment)


def _settings(db: Session, org) -> ReviewSettingsIO:
    required, source = reviews.required_source(db, org)
    return ReviewSettingsIO(review_required=org.review_required, validator_user_ids=sorted(reviews.designated(db, org)),
                            effective_required=required, source_org_slug=source.slug if source else None)


@router.get("/review-settings", response_model=ReviewSettingsIO, summary="Réglage de validation métier et validateurs (editor+)")
def get_settings(principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    return _settings(db, principal.org)


@router.put("/review-settings", response_model=ReviewSettingsIO,
            summary="Remplacer le réglage propre (None = hériter) et les validateurs désignés (org_admin)")
def put_settings(body: ReviewSettingsIO, principal: Principal = Depends(require_role("org_admin")),
                 db: Session = Depends(get_db)):
    org = principal.org
    members = {m["user_id"] for m in tenancy.list_members(db, org.id)}
    unknown = set(body.validator_user_ids) - members
    if unknown:
        raise HTTPException(status_code=400, detail=f"Membres inconnus : {sorted(unknown)}.")
    reviews.set_required(db, org, body.review_required)
    for uid in members:
        reviews.set_validator(db, org, uid, uid in set(body.validator_user_ids))
    audit.record(db, user_id=principal.user_id, org_id=org.id, action="update", entity_type="review_settings",
                 entity_id=org.id, meta={"review_required": body.review_required,
                                         "validators": sorted(body.validator_user_ids), **principal.audit_meta()})
    return _settings(db, org)
