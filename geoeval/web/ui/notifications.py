"""Notifications de l'utilisateur et réglages des détecteurs d'une entité (E7, ADR-089 §2.8).

Router UI mince : les règles vivent dans `geoeval.web.notifications` et
`geoeval.web.detectors`. La boîte de réception est personnelle (toutes entités).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from geoeval.db.models import Notification, Organization
from geoeval.web import audit, detectors, notifications
from geoeval.web.auth import CurrentUser
from geoeval.web.deps import get_db, require_role, require_user
from geoeval.web.rendering import nav_fallback, render

router = APIRouter()


@router.get("/notifications", response_class=HTMLResponse)
def inbox(request: Request, user: CurrentUser = Depends(require_user), db: Session = Depends(get_db),
          unread: int = 0):
    items = notifications.list_for_user(db, user.id, unread_only=bool(unread))
    orgs = {o.id: o for o in db.query(Organization).filter(
        Organization.id.in_({n.organization_id for n in items if n.organization_id})).all()} if items else {}
    nav_org, nav_role = nav_fallback(db, user)
    return render(request, "notifications.html", active="notifications", org=nav_org, role=nav_role,
                  items=items, orgs=orgs, kinds=notifications.KINDS, unread_only=bool(unread))


@router.post("/notifications/read-all")
def read_all(user: CurrentUser = Depends(require_user), db: Session = Depends(get_db)):
    notifications.mark_read(db, user.id)
    return RedirectResponse("/notifications", status_code=303)


@router.post("/notifications/{notification_id}/read")
def read_one(notification_id: int, user: CurrentUser = Depends(require_user), db: Session = Depends(get_db)):
    notifications.mark_read(db, user.id, notification_id)
    return RedirectResponse("/notifications", status_code=303)


@router.get("/notifications/{notification_id}/open")
def open_one(notification_id: int, user: CurrentUser = Depends(require_user), db: Session = Depends(get_db)):
    """Marque comme lue puis suit le lien (interne uniquement : pas de redirection ouverte)."""
    n = db.get(Notification, notification_id)
    if n is None or n.user_id != user.id:
        raise HTTPException(status_code=404, detail="Notification introuvable.")
    notifications.mark_read(db, user.id, notification_id)
    target = n.link if n.link and n.link.startswith("/") and not n.link.startswith("//") else "/notifications"
    return RedirectResponse(target, status_code=303)


@router.get("/notifications/preferences", response_class=HTMLResponse)
def preferences_form(request: Request, user: CurrentUser = Depends(require_user), db: Session = Depends(get_db)):
    nav_org, nav_role = nav_fallback(db, user)
    return render(request, "notification_preferences.html", active="notifications", org=nav_org, role=nav_role,
                  kinds=notifications.KINDS, prefs=notifications.preferences(db, user.id))


@router.post("/notifications/preferences")
def preferences_submit(user: CurrentUser = Depends(require_user), db: Session = Depends(get_db),
                       email_kinds: list[str] = Form(default=[])):
    unknown = set(email_kinds) - set(notifications.KINDS)
    if unknown:
        raise HTTPException(status_code=400, detail=f"Types inconnus : {sorted(unknown)}")
    notifications.set_preferences(db, user.id, {k: k in email_kinds for k in notifications.KINDS})
    return RedirectResponse("/notifications/preferences", status_code=303)


@router.get("/o/{org_slug}/settings/detectors", response_class=HTMLResponse)
def detectors_form(request: Request, ctx=Depends(require_role("org_admin")), db: Session = Depends(get_db)):
    org, role = ctx
    defaults = detectors.platform_defaults()
    return render(request, "detector_settings.html", active="settings", org=org, role=role,
                  own=detectors.own_settings(db, org.id), effective=detectors.effective_settings(db, org),
                  default_runs=defaults[0], default_threshold=defaults[1])


@router.post("/o/{org_slug}/settings/detectors")
def detectors_submit(ctx=Depends(require_role("org_admin")), db: Session = Depends(get_db),
                     user: CurrentUser = Depends(require_user),
                     always_wrong_runs: str = Form(""), always_wrong_threshold: str = Form("")):
    org, _ = ctx
    try:
        runs = int(always_wrong_runs) if always_wrong_runs.strip() else None
        detectors.set_settings(db, org, runs=runs, threshold=always_wrong_threshold or None, updated_by=user.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit.record(db, user_id=user.id, org_id=org.id, action="update", entity_type="detector_settings",
                 entity_id=org.id, meta={"always_wrong_runs": always_wrong_runs or None,
                                         "always_wrong_threshold": always_wrong_threshold or None})
    return RedirectResponse(f"/o/{org.slug}/settings/detectors", status_code=303)
