"""Paramètres d'organisation : membres, invitations, liste blanche de modèles, budget, audit.

Router UI (lot 1.3a) : découpage mécanique de l'ancien app.py, sans changement
de comportement. Les règles métier vivent dans les services (geoeval.web.*).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from geoeval.db.models import AuditLog, User
from geoeval.web import (
    audit,
    budget,
    launching,
    org_models,
    services,
    tenancy,
)
from geoeval.web.auth import CurrentUser
from geoeval.web.deps import (
    get_db,
    require_role,
    require_user,
)
from geoeval.web.rendering import render

router = APIRouter()




@router.get("/o/{org_slug}/settings", response_class=HTMLResponse)
def org_settings(
    request: Request,
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
):
    org, role = ctx
    return render(
        request,
        "org_settings.html",
        active="settings",
        org=org,
        role=role,
        members=tenancy.list_members(db, org.id),
        invitations=tenancy.list_invitations(db, org.id),
        roles=tenancy.ROLES,
    )


# ---- Allowlist de modèles (EPIC-001 Phase 4, S4.2 — org_admin) -------
@router.get("/o/{org_slug}/settings/models", response_class=HTMLResponse)
def org_models_page(
    request: Request,
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
):
    org, role = ctx
    catalog = services.list_models(db)  # catalogue global actif
    allowed = org_models.allowed_model_ids(db, org.id)  # None = héritage
    inherits = allowed is None
    checked_ids = {m.model_id for m in catalog} if inherits else allowed
    return render(
        request, "org_models.html", active="settings", org=org, role=role,
        catalog=catalog, checked_ids=checked_ids, inherits=inherits,
        testable_providers=launching.TESTABLE_PROVIDERS,
    )


@router.post("/o/{org_slug}/settings/models")
def org_models_save(
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
    model_ids: list[int] = Form(default=[]),
):
    org, _ = ctx
    org_models.replace_allowlist(db, org.id, set(model_ids))
    audit.record(
        db, user_id=user.id, org_id=org.id,
        action="set_allowlist", entity_type="org_models", entity_id=None,
        meta={"model_ids": sorted(set(model_ids))},
    )
    return RedirectResponse(f"/o/{org.slug}/settings/models", status_code=303)


@router.post("/o/{org_slug}/settings/models/reset")
def org_models_reset(
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
):
    org, _ = ctx
    org_models.clear_allowlist(db, org.id)
    audit.record(
        db, user_id=user.id, org_id=org.id,
        action="clear_allowlist", entity_type="org_models", entity_id=None,
    )
    return RedirectResponse(f"/o/{org.slug}/settings/models", status_code=303)


@router.post("/o/{org_slug}/settings/rename")
def org_rename(
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
    name: str = Form(...),
):
    org, _ = ctx
    old = org.name
    tenancy.rename_org(db, org.id, name=name)
    audit.record(
        db,
        user_id=user.id,
        org_id=org.id,
        action="rename",
        entity_type="organization",
        entity_id=org.id,
        meta={"old": old, "new": name},
    )
    return RedirectResponse(f"/o/{org.slug}/settings", status_code=303)


@router.post("/o/{org_slug}/members/{user_id}/role")
def member_change_role(
    user_id: int,
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
    role: str = Form(...),
):
    org, _ = ctx
    try:
        tenancy.set_membership_role(db, user_id=user_id, org_id=org.id, role=role)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit.record(
        db,
        user_id=user.id,
        org_id=org.id,
        action="set_role",
        entity_type="membership",
        entity_id=user_id,
        meta={"role": role},
    )
    return RedirectResponse(f"/o/{org.slug}/settings", status_code=303)


@router.post("/o/{org_slug}/members/{user_id}/remove")
def member_remove(
    user_id: int,
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
):
    org, _ = ctx
    if user_id == user.id:
        raise HTTPException(status_code=400, detail="Impossible de te retirer toi-même.")
    tenancy.remove_membership(db, user_id=user_id, org_id=org.id)
    audit.record(
        db,
        user_id=user.id,
        org_id=org.id,
        action="remove",
        entity_type="membership",
        entity_id=user_id,
    )
    return RedirectResponse(f"/o/{org.slug}/settings", status_code=303)


@router.post("/o/{org_slug}/invitations")
def invitation_create(
    request: Request,
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
    email: str = Form(...),
    role: str = Form(...),
):
    org, _ = ctx
    if role not in tenancy.ROLES:
        raise HTTPException(status_code=400, detail=f"Rôle invalide : {role!r}.")
    inv = tenancy.create_invitation(
        db, org_id=org.id, email=email, role=role, invited_by=user.id
    )
    audit.record(
        db,
        user_id=user.id,
        org_id=org.id,
        action="create",
        entity_type="invitation",
        entity_id=inv.id,
        meta={"email": inv.email, "role": role},
    )
    return RedirectResponse(f"/o/{org.slug}/settings", status_code=303)


@router.post("/o/{org_slug}/invitations/{inv_id}/revoke")
def invitation_revoke(
    inv_id: int,
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
):
    org, _ = ctx
    try:
        tenancy.revoke_invitation(db, org.id, inv_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit.record(
        db,
        user_id=user.id,
        org_id=org.id,
        action="revoke",
        entity_type="invitation",
        entity_id=inv_id,
    )
    return RedirectResponse(f"/o/{org.slug}/settings", status_code=303)


@router.get("/o/{org_slug}/accept-invite", response_class=HTMLResponse)
def accept_invite_form(
    org_slug: str,
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
    token: str = "",
):
    """Landing d'acceptation d'invitation.

    Volontairement PAS derrière `require_org` : un invité qui n'est pas encore
    membre doit pouvoir atterrir sur cette page (sinon 404 avant qu'il puisse
    confirmer). L'org est résolue à la volée, sans vérif de membership.
    """
    org = tenancy.get_org_by_slug(db, org_slug)
    if org is None:
        raise HTTPException(status_code=404, detail="Organisation introuvable.")
    return render(
        request,
        "accept_invite.html",
        active="settings",
        org=org,
        role=None,
        token=token,
        current_email=user.email,
    )


@router.post("/o/{org_slug}/accept-invite")
def accept_invite_submit(
    org_slug: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
    token: str = Form(...),
):
    org = tenancy.get_org_by_slug(db, org_slug)
    if org is None:
        raise HTTPException(status_code=404, detail="Organisation introuvable.")
    try:
        m = tenancy.accept_invitation(db, token=token, current_email=user.email)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    if m.org_id != org.id:
        raise HTTPException(status_code=400, detail="Token pour une autre organisation.")
    audit.record(
        db,
        user_id=user.id,
        org_id=org.id,
        action="accept",
        entity_type="invitation",
        entity_id=None,
        meta={"role": m.role},
    )
    return RedirectResponse(f"/o/{org.slug}/", status_code=303)


@router.get("/o/{org_slug}/audit", response_class=HTMLResponse)
def org_audit_view(
    request: Request,
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
    page: int = 0,
):
    org, role = ctx
    PAGE_SIZE = 50
    rows = db.execute(
        select(AuditLog, User.email)
        .outerjoin(User, User.id == AuditLog.user_id)
        .where(AuditLog.org_id == org.id)
        .order_by(AuditLog.at.desc())
        .offset(page * PAGE_SIZE)
        .limit(PAGE_SIZE)
    ).all()
    entries = [
        dict(
            id=al.id, at=al.at, action=al.action, entity_type=al.entity_type,
            entity_id=al.entity_id, meta=al.meta_json, actor=email or "—",
        )
        for al, email in rows
    ]
    return render(
        request, "audit.html", active="settings", org=org, role=role,
        entries=entries, page=page, next_page=page + 1 if len(entries) == PAGE_SIZE else None,
    )



@router.get("/o/{org_slug}/budget", response_class=HTMLResponse)
def org_budget_view(
    request: Request,
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
):
    org, role = ctx
    b = budget.get_budget(db, org.id)
    spent = budget.current_period_spent(db, org.id, "month")
    day_spent = budget.current_period_spent(db, org.id, "day")
    pct = None
    if b is not None and b.monthly_cap_eur:
        pct = min(100, int((spent / Decimal(str(b.monthly_cap_eur))) * 100))
    pct_day = None
    if b is not None and b.daily_cap_eur:
        pct_day = min(100, int((day_spent / Decimal(str(b.daily_cap_eur))) * 100))
    return render(
        request, "org_budget.html", active="settings", org=org, role=role,
        budget=b, month_spent=spent, day_spent=day_spent, pct=pct, pct_day=pct_day,
    )


@router.post("/o/{org_slug}/budget")
def org_budget_set(
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
    monthly_cap_eur: str = Form(...),
    daily_cap_eur: str = Form(""),
):
    org, _ = ctx
    try:
        cap = Decimal(monthly_cap_eur.replace(",", "."))
        if cap < 0:
            raise ValueError("négatif")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Cap invalide : {e}")
    daily_cap: Optional[Decimal] = None
    if daily_cap_eur.strip():
        try:
            daily_cap = Decimal(daily_cap_eur.replace(",", "."))
            if daily_cap < 0:
                raise ValueError("négatif")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"Cap journalier invalide : {e}")
    budget.set_cap(
        db, org_id=org.id, cap_eur=cap, updated_by=user.id, daily_cap_eur=daily_cap
    )
    audit.record(
        db,
        user_id=user.id, org_id=org.id, action="set_cap",
        entity_type="budget", entity_id=org.id,
        meta={"cap_eur": str(cap), "daily_cap_eur": str(daily_cap) if daily_cap is not None else None},
    )
    return RedirectResponse(f"/o/{org.slug}/budget", status_code=303)
