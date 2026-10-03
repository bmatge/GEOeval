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
    api_tokens,
    budget_alerts,
    mailer,
    hierarchy,
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
    return _render_settings(request, db, org, role)


def _render_settings(request, db, org, role, *, new_token: Optional[str] = None, token_error: Optional[str] = None):
    tokens = api_tokens.list_for_org(db, org.id)
    return render(
        request,
        "org_settings.html",
        active="settings",
        org=org,
        role=role,
        members=tenancy.list_members(db, org.id),
        invitations=tenancy.list_invitations(db, org.id),
        roles=tenancy.ROLES,
        api_tokens=[(t, api_tokens.is_valid(t)) for t in tokens],
        lineage=hierarchy.lineage(db, org),
        kind_label=hierarchy.KIND_LABELS.get(org.kind, org.kind),
        inherited_members=tenancy.list_inherited_members(db, org),
        n_children=len(hierarchy.children(db, org.id)),
        new_token=new_token,
        token_error=token_error,
    )


# ---- Jetons d'API (lot 1.3b — org_admin) -----------------------------
@router.post("/o/{org_slug}/settings/tokens", response_class=HTMLResponse)
def api_token_create(
    request: Request,
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
    name: str = Form(...),
    token_role: str = Form("viewer"),
    expires_in_days: str = Form(""),
):
    """Crée un jeton et l'affiche UNE fois (pas de redirection : le clair ne
    doit pas transiter dans une URL)."""
    org, role = ctx
    try:
        token, plaintext = api_tokens.create(
            db, org_id=org.id, name=name, role=token_role, created_by=user.id,
            expires_in_days=int(expires_in_days) if expires_in_days.strip() else None,
        )
    except ValueError as exc:
        return _render_settings(request, db, org, role, token_error=str(exc))
    audit.record(db, user_id=user.id, org_id=org.id, action="create", entity_type="api_token",
                 entity_id=token.id, meta={"name": token.name, "role": token.role, "prefix": token.prefix})
    return _render_settings(request, db, org, role, new_token=plaintext)


@router.post("/o/{org_slug}/settings/tokens/{token_id}/revoke")
def api_token_revoke(
    token_id: int,
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
):
    org, _ = ctx
    try:
        token = api_tokens.revoke(db, org.id, token_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    audit.record(db, user_id=user.id, org_id=org.id, action="revoke", entity_type="api_token",
                 entity_id=token.id, meta={"prefix": token.prefix})
    return RedirectResponse(f"/o/{org.slug}/settings", status_code=303)


# ---- Allowlist de modèles (EPIC-001 Phase 4, S4.2 — org_admin) -------
@router.get("/o/{org_slug}/settings/models", response_class=HTMLResponse)
def org_models_page(
    request: Request,
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
):
    org, role = ctx
    # Catalogue proposé : ce que les entités parentes autorisent (ADR-089 §2.2).
    catalog = org_models.filter_models(db, org.id, services.list_models(db), include_own=False)
    inherited = org_models.resolve_allowed_ids(db, org.id, include_own=False)
    allowed = org_models.allowed_model_ids(db, org.id)  # liste propre ; None = héritage
    inherits = allowed is None
    checked_ids = {m.model_id for m in catalog} if inherits else allowed
    return render(
        request, "org_models.html", active="settings", org=org, role=role,
        catalog=catalog, checked_ids=checked_ids, inherits=inherits,
        inherited_sources=inherited.sources,
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
    constraints = budget.chain_constraints(db, org)
    return render(
        request, "org_budget.html", active="settings", org=org, role=role,
        budget=budget.get_budget(db, org.id),
        own=[c for c in constraints if not c.inherited],
        inherited=[c for c in constraints if c.inherited],
        budget_alerts=[c for c in constraints if c.level != "ok"],
        month_spent=budget.subtree_period_spent(db, org, "month"),
        day_spent=budget.subtree_period_spent(db, org, "day"),
        own_month_spent=budget.own_period_spent(db, org.id, "month"),
        n_descendants=len(hierarchy.descendants(db, org)),
        alerts=budget_alerts.recent_for_chain(db, org),
        alert_owner_names={o.id: o.name for o in hierarchy.chain(db, org)},
        smtp_configured=mailer.is_configured(),
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


# ---- Sous-entités : délégation de la structure (ADR-089 §2.4, chantier E2) --
def _anchors(user: CurrentUser) -> dict[int, str]:
    return dict(user.memberships)


def _subtree_target(db: Session, org, target_id: int):
    """Entité STRICTEMENT sous `org`, sinon 404 (pas de divulgation hors périmètre)."""
    target = tenancy.get_org(db, target_id)
    if target is None or target.id == org.id or not hierarchy.is_ancestor_or_self(org, target):
        raise HTTPException(status_code=404, detail="Sous-entité introuvable.")
    return target


def _subtree_parent(db: Session, org, raw: str):
    """Parent choisi dans le sous-arbre de `org` (org comprise), sinon 400."""
    raw = (raw or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Choisissez une entité parente dans votre périmètre.")
    parent = tenancy.get_org(db, int(raw))
    if parent is None or not hierarchy.is_ancestor_or_self(org, parent):
        raise HTTPException(status_code=400, detail="L'entité parente doit appartenir au périmètre de cette organisation.")
    return parent


@router.get("/o/{org_slug}/settings/entities", response_class=HTMLResponse)
def subentities_page(
    request: Request,
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
):
    org, role = ctx
    subtree = hierarchy.descendants(db, org, include_self=True)
    return render(
        request, "org_entities.html", active="settings", org=org, role=role,
        subtree=hierarchy.tree(subtree), root_depth=org.depth,
        parents=[o for o in subtree if o.depth < hierarchy.MAX_DEPTH],
        editable={o.id for o in subtree if tenancy.can_qualify(_anchors(user), user.is_platform_admin, o)},
        kinds=hierarchy.ORG_KINDS, kind_labels=hierarchy.KIND_LABELS,
    )


@router.post("/o/{org_slug}/settings/entities")
def subentity_create(
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
    name: str = Form(...),
    slug: str = Form(...),
    parent_id: str = Form(...),
    kind: str = Form("autre"),
    siret: str = Form(""),
):
    org, _ = ctx
    parent = _subtree_parent(db, org, parent_id)
    if not tenancy.can_create_under(_anchors(user), user.is_platform_admin, parent):
        raise HTTPException(status_code=403, detail="Rôle org_admin requis sur l'entité parente.")
    try:
        child = tenancy.create_org(db, name=name, slug=slug, created_by=user.id, parent=parent, kind=kind, siret=siret)
    except hierarchy.HierarchyError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    audit.record(db, user_id=user.id, org_id=child.id, action="create", entity_type="organization", entity_id=child.id,
                 meta={"slug": child.slug, "kind": child.kind, "parent_id": child.parent_id, "delegated_from": org.id})
    return RedirectResponse(f"/o/{org.slug}/settings/entities", status_code=303)


@router.get("/o/{org_slug}/settings/entities/{target_id}/edit", response_class=HTMLResponse)
def subentity_edit_form(
    target_id: int,
    request: Request,
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
):
    org, role = ctx
    target = _subtree_target(db, org, target_id)
    if not tenancy.can_qualify(_anchors(user), user.is_platform_admin, target):
        raise HTTPException(status_code=403, detail="Rôle org_admin requis sur une entité ancêtre de celle-ci.")
    forbidden = set(hierarchy.descendant_ids(db, target, include_self=True))
    candidates = [o for o in hierarchy.tree(hierarchy.descendants(db, org, include_self=True)) if o.id not in forbidden]
    return render(
        request, "admin_organization_edit.html", active="settings", org=org, role=role,
        target=target, lineage=hierarchy.lineage(db, target), children=hierarchy.children(db, target.id),
        candidates=candidates, kinds=hierarchy.ORG_KINDS, kind_labels=hierarchy.KIND_LABELS,
        form_action=f"/o/{org.slug}/settings/entities/{target.id}/edit",
        back_url=f"/o/{org.slug}/settings/entities", allow_root=False,
    )


@router.post("/o/{org_slug}/settings/entities/{target_id}/edit")
def subentity_edit_submit(
    target_id: int,
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
    name: str = Form(...),
    kind: str = Form("autre"),
    siret: str = Form(""),
    parent_id: str = Form(""),
):
    org, _ = ctx
    target = _subtree_target(db, org, target_id)
    new_parent = _subtree_parent(db, org, parent_id)
    anchors = _anchors(user)
    if not tenancy.can_qualify(anchors, user.is_platform_admin, target):
        raise HTTPException(status_code=403, detail="Rôle org_admin requis sur une entité ancêtre de celle-ci.")
    if not tenancy.can_restructure(anchors, user.is_platform_admin, target, new_parent):
        raise HTTPException(status_code=403, detail="Rattachement hors de votre périmètre.")
    old_parent = target.parent_id
    try:
        hierarchy.update_and_move(db, target, name=name, kind=kind, siret=siret, set_siret=True,
                                  move_to=new_parent, do_move=True)
    except hierarchy.HierarchyError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    audit.record(db, user_id=user.id, org_id=target.id, action="update", entity_type="organization", entity_id=target.id,
                 meta={"kind": target.kind, "parent_from": old_parent, "parent_to": target.parent_id, "delegated_from": org.id})
    return RedirectResponse(f"/o/{org.slug}/settings/entities", status_code=303)
