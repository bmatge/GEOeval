"""Signalements humains sur les questions (suite E7, ADR-089 §2.8).

Router UI mince : les règles vivent dans `geoeval.web.reports`. Signaler : tout membre
d'une entité qui voit la question. Traiter : éditeur+ de l'entité propriétaire.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from geoeval.db.models import Test
from geoeval.web import audit, reports
from geoeval.web.auth import CurrentUser
from geoeval.web.deps import get_db, require_role, require_user
from geoeval.web.rendering import render
from geoeval.web.tenancy import role_at_least

router = APIRouter()


def _http(exc: reports.ReportError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


@router.get("/o/{org_slug}/tests/{test_id}/report", response_class=HTMLResponse)
def report_form(test_id: int, request: Request, ctx=Depends(require_role("viewer")), db: Session = Depends(get_db),
                run_id: Optional[int] = None):
    org, role = ctx
    test = db.get(Test, test_id)
    if test is None or not reports.can_see_test(db, org, test):
        raise HTTPException(status_code=404, detail="Question introuvable.")
    return render(request, "report_form.html", active="reports", org=org, role=role, test=test, run_id=run_id,
                  categories=reports.CATEGORIES)


@router.post("/o/{org_slug}/tests/{test_id}/report")
def report_submit(test_id: int, ctx=Depends(require_role("viewer")), db: Session = Depends(get_db),
                  user: CurrentUser = Depends(require_user), category: str = Form(...), comment: str = Form(""),
                  run_id: str = Form("")):
    org, _ = ctx
    if run_id.strip() and not run_id.strip().isdigit():
        raise HTTPException(status_code=400, detail="Run invalide.")
    try:
        r = reports.create(db, org, test_id=test_id, category=category, comment=comment, reporter_user_id=user.id,
                           run_id=int(run_id) if run_id.strip() else None)
    except reports.ReportError as e:
        raise _http(e)
    audit.record(db, user_id=user.id, org_id=org.id, action="create", entity_type="test_report", entity_id=r.id,
                 meta={"test_id": test_id, "category": category})
    return RedirectResponse(f"/o/{org.slug}/reports/{r.id}", status_code=303)


@router.get("/o/{org_slug}/reports", response_class=HTMLResponse)
def reports_list(request: Request, ctx=Depends(require_role("viewer")), db: Session = Depends(get_db),
                 tab: str = "received", status: str = ""):
    org, role = ctx
    items = reports.list_sent(db, org) if tab == "sent" else reports.list_received(db, org, status=status or None)
    return render(request, "reports.html", active="reports", org=org, role=role, items=items,
                  tab="sent" if tab == "sent" else "received", status=status, categories=reports.CATEGORIES,
                  status_labels=reports.STATUS_LABELS, open_count=reports.open_count_for_org(db, org.id),
                  **reports.context(db, items))


@router.get("/o/{org_slug}/reports/{report_id}", response_class=HTMLResponse)
def report_detail(report_id: int, request: Request, ctx=Depends(require_role("viewer")), db: Session = Depends(get_db)):
    org, role = ctx
    try:
        r, owned = reports.get_for_org(db, org, report_id)
    except reports.ReportError as e:
        raise _http(e)
    return render(request, "report_detail.html", active="reports", org=org, role=role, report=r, owned=owned,
                  can_resolve=owned and role_at_least(role, "editor") and r.status == "open",
                  categories=reports.CATEGORIES, status_labels=reports.STATUS_LABELS, **reports.context(db, [r]))


@router.post("/o/{org_slug}/reports/{report_id}/resolve")
def report_resolve(report_id: int, ctx=Depends(require_role("editor")), db: Session = Depends(get_db),
                   user: CurrentUser = Depends(require_user), status: str = Form(...), comment: str = Form("")):
    org, _ = ctx
    try:
        r, owned = reports.get_for_org(db, org, report_id)
        if not owned:
            raise reports.ReportError("Seule l'entité propriétaire de la question traite le signalement.", 403)
        reports.resolve(db, r, status=status, comment=comment, resolved_by=user.id)
    except reports.ReportError as e:
        raise _http(e)
    audit.record(db, user_id=user.id, org_id=org.id, action="resolve", entity_type="test_report", entity_id=r.id,
                 meta={"status": status})
    return RedirectResponse(f"/o/{org.slug}/reports/{r.id}", status_code=303)
