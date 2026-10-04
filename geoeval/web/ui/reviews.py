"""Validation métier des questions (fin de E8, ADR-089 §2.9).

Router UI mince : les règles vivent dans `geoeval.web.reviews`. Relectures en attente :
membres de l'entité. Approuver / renvoyer : validateurs (hors auteur de la soumission).
Réglage et désignation des validateurs : org_admin.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from geoeval.db.models import User
from geoeval.web import audit, reviews, services, tenancy
from geoeval.web.auth import CurrentUser
from geoeval.web.deps import get_db, require_role, require_user
from geoeval.web.rendering import render

router = APIRouter()


def _http(exc: reviews.ReviewError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


@router.get("/o/{org_slug}/reviews", response_class=HTMLResponse)
def reviews_list(request: Request, ctx=Depends(require_role("viewer")), db: Session = Depends(get_db),
                 user: CurrentUser = Depends(require_user)):
    org, role = ctx
    items = reviews.pending(db, org)
    authors = {u.id: u for u in db.execute(select(User).where(
        User.id.in_({t.submitted_by for t in items if t.submitted_by} or {-1}))).scalars()}
    required, source = reviews.required_source(db, org)
    return render(request, "reviews.html", active="reviews", org=org, role=role, items=items, authors=authors,
                  required=required, source=source, me=user.id,
                  can_review=reviews.is_validator(db, org, user.id, is_platform_admin=user.is_platform_admin))


def _decide(db: Session, org, user: CurrentUser, test_id: int, approve: bool, comment: str = ""):
    test = services.get_test(db, org.id, test_id)
    if test is None:
        raise HTTPException(status_code=404, detail="Question introuvable.")
    try:
        if approve:
            reviews.approve(db, org, test, user.id, is_platform_admin=user.is_platform_admin)
        else:
            reviews.reject(db, org, test, user.id, comment, is_platform_admin=user.is_platform_admin)
    except reviews.ReviewError as e:
        raise _http(e)
    audit.record(db, user_id=user.id, org_id=org.id, action="approve" if approve else "reject", entity_type="test",
                 entity_id=test_id, meta={} if approve else {"comment": test.review_comment})


@router.post("/o/{org_slug}/tests/{test_id}/approve")
def test_approve(test_id: int, ctx=Depends(require_role("viewer")), db: Session = Depends(get_db),
                 user: CurrentUser = Depends(require_user)):
    org, _ = ctx
    _decide(db, org, user, test_id, True)
    return RedirectResponse(f"/o/{org.slug}/reviews", status_code=303)


@router.post("/o/{org_slug}/tests/{test_id}/reject")
def test_reject(test_id: int, ctx=Depends(require_role("viewer")), db: Session = Depends(get_db),
                user: CurrentUser = Depends(require_user), comment: str = Form("")):
    org, _ = ctx
    _decide(db, org, user, test_id, False, comment)
    return RedirectResponse(f"/o/{org.slug}/reviews", status_code=303)


@router.get("/o/{org_slug}/settings/reviews", response_class=HTMLResponse)
def review_settings(request: Request, ctx=Depends(require_role("org_admin")), db: Session = Depends(get_db)):
    org, role = ctx
    required, source = reviews.required_source(db, org)
    return render(request, "review_settings.html", active="settings", org=org, role=role, required=required,
                  source=source, own=org.review_required, members=tenancy.list_members(db, org.id),
                  designated=reviews.designated(db, org))


@router.post("/o/{org_slug}/settings/reviews")
def review_settings_submit(ctx=Depends(require_role("org_admin")), db: Session = Depends(get_db),
                           user: CurrentUser = Depends(require_user), review_required: str = Form("inherit"),
                           validator_ids: list[int] = Form(default=[])):
    org, _ = ctx
    if review_required not in ("inherit", "yes", "no"):
        raise HTTPException(status_code=400, detail="Réglage inconnu.")
    reviews.set_required(db, org, None if review_required == "inherit" else review_required == "yes")
    members = {m["user_id"] for m in tenancy.list_members(db, org.id)}
    unknown = set(validator_ids) - members
    if unknown:
        raise HTTPException(status_code=400, detail=f"Membres inconnus : {sorted(unknown)}.")
    for uid in members:
        reviews.set_validator(db, org, uid, uid in set(validator_ids))
    audit.record(db, user_id=user.id, org_id=org.id, action="update", entity_type="review_settings", entity_id=org.id,
                 meta={"review_required": org.review_required, "validators": sorted(validator_ids)})
    return RedirectResponse(f"/o/{org.slug}/settings/reviews", status_code=303)
