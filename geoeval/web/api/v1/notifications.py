"""Notifications de l'utilisateur et réglages des détecteurs (E7, ADR-089 §2.8).

Les notifications sont personnelles : session uniquement (un jeton d'organisation
n'a pas de boîte de réception → 403). Réglages des détecteurs : lecture editor+,
écriture org_admin.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from geoeval.db.models import ApiToken, Notification
from geoeval.web import audit, detectors, notifications
from geoeval.web.api.deps import Principal, current_token, require_role, session_user
from geoeval.web.api.schemas import (
    DetectorSettingsIn,
    DetectorSettingsOut,
    NotificationListOut,
    NotificationOut,
    NotificationPreferencesIO,
)
from geoeval.web.deps import get_db

router = APIRouter(tags=["notifications"])


def _me(request: Request, token: Optional[ApiToken] = Depends(current_token)):
    if token is not None:
        raise HTTPException(status_code=403, detail="Notifications personnelles : session requise (jeton d'organisation refusé).")
    user = session_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentification requise (session).")
    return user


@router.get("/me/notifications", response_model=NotificationListOut, summary="Mes notifications (plus récentes d'abord)")
def my_notifications(user=Depends(_me), db: Session = Depends(get_db), unread_only: bool = False,
                     limit: int = Query(100, ge=1, le=500)):
    items = notifications.list_for_user(db, user.id, unread_only=unread_only, limit=limit)
    return NotificationListOut(unread=notifications.unread_count(db, user.id),
                               items=[NotificationOut.model_validate(n) for n in items])


@router.post("/me/notifications/{notification_id}/read", response_model=NotificationListOut,
             summary="Marquer une notification comme lue")
def read_one(notification_id: int, user=Depends(_me), db: Session = Depends(get_db)):
    n = db.get(Notification, notification_id)
    if n is None or n.user_id != user.id:
        raise HTTPException(status_code=404, detail="Notification introuvable.")
    notifications.mark_read(db, user.id, notification_id)
    return my_notifications(user, db, False, 100)


@router.post("/me/notifications/read-all", response_model=NotificationListOut, summary="Tout marquer comme lu")
def read_all(user=Depends(_me), db: Session = Depends(get_db)):
    notifications.mark_read(db, user.id)
    return my_notifications(user, db, False, 100)


@router.get("/me/notification-preferences", response_model=NotificationPreferencesIO,
            summary="Mes préférences d'email par type")
def get_prefs(user=Depends(_me), db: Session = Depends(get_db)):
    return NotificationPreferencesIO(email=notifications.preferences(db, user.id))


@router.put("/me/notification-preferences", response_model=NotificationPreferencesIO,
            summary="Modifier mes préférences (types omis : inchangés)")
def put_prefs(body: NotificationPreferencesIO, user=Depends(_me), db: Session = Depends(get_db)):
    try:
        return NotificationPreferencesIO(email=notifications.set_preferences(db, user.id, body.email))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _settings_out(db: Session, org) -> DetectorSettingsOut:
    own = detectors.own_settings(db, org.id)
    eff = detectors.effective_settings(db, org)
    return DetectorSettingsOut(
        own=DetectorSettingsIn(always_wrong_runs=own.always_wrong_runs if own else None,
                               always_wrong_threshold=own.always_wrong_threshold if own else None),
        effective_runs=eff.runs, effective_threshold=eff.threshold,
        runs_from_org_slug=eff.runs_from.slug if eff.runs_from else None,
        threshold_from_org_slug=eff.threshold_from.slug if eff.threshold_from else None,
    )


@router.get("/orgs/{org_slug}/detector-settings", response_model=DetectorSettingsOut,
            summary="Réglages du détecteur « toujours faux » : propres et effectifs (editor+)")
def get_detector_settings(principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    return _settings_out(db, principal.org)


@router.put("/orgs/{org_slug}/detector-settings", response_model=DetectorSettingsOut,
            summary="Remplacer les réglages propres de l'entité (org_admin) — None = hériter")
def put_detector_settings(body: DetectorSettingsIn, principal: Principal = Depends(require_role("org_admin")),
                          db: Session = Depends(get_db)):
    try:
        detectors.set_settings(db, principal.org, runs=body.always_wrong_runs, threshold=body.always_wrong_threshold,
                               updated_by=principal.user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action="update", entity_type="detector_settings",
                 entity_id=principal.org.id, meta={**body.model_dump(mode="json"), **principal.audit_meta()})
    return _settings_out(db, principal.org)
