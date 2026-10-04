"""Signalements humains sur les questions (suite E7, ADR-089 §2.8).

Tout membre (viewer+) d'une entité qui voit une question peut la signaler ; les
éditeurs de l'entité propriétaire la clôturent (corrigé / rejeté) avec une réponse.
Lecture : entité propriétaire (signalements reçus) ou auteure (signalements émis).
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from geoeval.web import audit, reports
from geoeval.web.api.deps import Principal, require_role
from geoeval.web.api.schemas import TestReportIn, TestReportOut, TestReportResolveIn
from geoeval.web.deps import get_db

router = APIRouter(prefix="/orgs/{org_slug}/reports", tags=["signalements"])


def _http(exc: reports.ReportError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


def _out(db: Session, items: list, org) -> list[TestReportOut]:
    ctx = reports.context(db, items)
    out = []
    for r in items:
        test, user = ctx["tests"].get(r.test_id), ctx["users"].get(r.reporter_user_id)
        out.append(TestReportOut(
            id=r.id, test_id=r.test_id, test_prompt=test.prompt if test else None,
            owner_org_slug=ctx["orgs"][r.owner_org_id].slug, reporter_org_slug=ctx["orgs"][r.reporter_org_id].slug,
            reporter_email=user.email if user else None, run_id=r.run_id, category=r.category,
            comment=r.comment or "", status=r.status, resolution_comment=r.resolution_comment,
            resolved_at=r.resolved_at, created_at=r.created_at, owned=r.owner_org_id == org.id,
        ))
    return out


def _audit(db: Session, principal: Principal, action: str, report_id: int, **meta) -> None:
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action=action, entity_type="test_report",
                 entity_id=report_id, meta={**meta, **principal.audit_meta()})


@router.get("", response_model=list[TestReportOut], summary="Signalements reçus (questions de l'entité) ou émis")
def list_reports(principal: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db),
                 tab: Literal["received", "sent"] = "received",
                 status_: Optional[Literal["open", "fixed", "rejected"]] = Query(None, alias="status")):
    if tab == "sent":
        items = [r for r in reports.list_sent(db, principal.org) if status_ is None or r.status == status_]
    else:
        items = reports.list_received(db, principal.org, status=status_)
    return _out(db, items, principal.org)


@router.post("", response_model=TestReportOut, status_code=status.HTTP_201_CREATED,
             summary="Signaler une question visible de l'entité (viewer+)")
def create_report(body: TestReportIn, principal: Principal = Depends(require_role("viewer")),
                  db: Session = Depends(get_db)):
    try:
        r = reports.create(db, principal.org, test_id=body.test_id, category=body.category, comment=body.comment,
                           reporter_user_id=principal.user_id, run_id=body.run_id)
    except reports.ReportError as e:
        raise _http(e)
    _audit(db, principal, "create", r.id, test_id=r.test_id, category=r.category)
    return _out(db, [r], principal.org)[0]


@router.get("/{report_id}", response_model=TestReportOut, summary="Détail d'un signalement reçu ou émis")
def get_report(report_id: int, principal: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    try:
        r, _ = reports.get_for_org(db, principal.org, report_id)
    except reports.ReportError as e:
        raise _http(e)
    return _out(db, [r], principal.org)[0]


@router.post("/{report_id}/resolve", response_model=TestReportOut,
             summary="Clôturer un signalement reçu : corrigé ou rejeté, avec réponse (editor+)")
def resolve_report(report_id: int, body: TestReportResolveIn, principal: Principal = Depends(require_role("editor")),
                   db: Session = Depends(get_db)):
    try:
        r, owned = reports.get_for_org(db, principal.org, report_id)
        if not owned:
            raise reports.ReportError("Seule l'entité propriétaire de la question traite le signalement.", 403)
        reports.resolve(db, r, status=body.status, comment=body.comment, resolved_by=principal.user_id)
    except reports.ReportError as e:
        raise _http(e)
    _audit(db, principal, "resolve", r.id, status=r.status)
    return _out(db, [r], principal.org)[0]
