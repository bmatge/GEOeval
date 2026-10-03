"""Administration plateforme : organisations, utilisateurs, audit global, tarifs, import OpenRouter, gold set.

Router UI (lot 1.3a) : découpage mécanique de l'ancien app.py, sans changement
de comportement. Les règles métier vivent dans les services (geoeval.web.*).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from geoeval.db.models import AuditLog, Organization, User
from geoeval.db.session import SessionLocal
from geoeval.web import (
    accounts,
    audit,
    hierarchy,
    gold,
    openrouter_catalog,
    pricing,
    services,
    tenancy,
)
from geoeval.web.auth import CurrentUser
from geoeval.web.deps import (
    get_db,
    require_platform_admin,
)
from geoeval.web.rendering import nav_fallback, render

from geoeval.web.auth import load_or_provision_user

router = APIRouter()


@router.get("/admin/organizations", response_class=HTMLResponse)
def admin_orgs(
    request: Request,
    user: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    orgs = hierarchy.tree(tenancy.list_all_orgs(db))
    nav_org, nav_role = nav_fallback(db, user)
    return render(
        request, "admin_organizations.html", active="admin",
        org=nav_org, role=nav_role,
        orgs=orgs, roles=tenancy.ROLES,
        kinds=hierarchy.ORG_KINDS, kind_labels=hierarchy.KIND_LABELS, max_depth=hierarchy.MAX_DEPTH,
    )


def _parent_or_400(db: Session, raw: str):
    raw = (raw or "").strip()
    if not raw:
        return None
    parent = tenancy.get_org(db, int(raw))
    if parent is None:
        raise HTTPException(status_code=404, detail="Entité parente introuvable.")
    return parent


@router.get("/admin/organizations/{org_id}/edit", response_class=HTMLResponse)
def admin_org_edit_form(
    org_id: int,
    request: Request,
    user: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    target = tenancy.get_org(db, org_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Entité introuvable.")
    # Parents possibles : toute entité hors du sous-arbre de la cible (pas de cycle).
    forbidden = set(hierarchy.descendant_ids(db, target, include_self=True))
    candidates = [o for o in hierarchy.tree(tenancy.list_all_orgs(db)) if o.id not in forbidden]
    nav_org, nav_role = nav_fallback(db, user)
    return render(
        request, "admin_organization_edit.html", active="admin",
        org=nav_org, role=nav_role,
        target=target, lineage=hierarchy.lineage(db, target), children=hierarchy.children(db, target.id),
        candidates=candidates, kinds=hierarchy.ORG_KINDS, kind_labels=hierarchy.KIND_LABELS,
    )


@router.post("/admin/organizations/{org_id}/edit")
def admin_org_edit_submit(
    org_id: int,
    user: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
    name: str = Form(...),
    kind: str = Form("autre"),
    siret: str = Form(""),
    parent_id: str = Form(""),
):
    target = tenancy.get_org(db, org_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Entité introuvable.")
    old_parent = target.parent_id
    new_parent = _parent_or_400(db, parent_id)
    try:
        hierarchy.update_and_move(db, target, name=name, kind=kind, siret=siret, set_siret=True,
                                  move_to=new_parent, do_move=True)
    except hierarchy.HierarchyError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    audit.record(
        db, user_id=user.id, org_id=target.id, action="update", entity_type="organization", entity_id=target.id,
        meta={"kind": target.kind, "siret": target.siret, "parent_from": old_parent, "parent_to": target.parent_id},
    )
    return RedirectResponse("/admin/organizations", status_code=303)


@router.post("/admin/organizations/new")
def admin_org_create(
    user: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
    name: str = Form(...),
    slug: str = Form(...),
    first_admin_email: str = Form(""),
    parent_id: str = Form(""),
    kind: str = Form("autre"),
    siret: str = Form(""),
):
    parent = _parent_or_400(db, parent_id)
    try:
        org = tenancy.create_org(db, name=name, slug=slug, created_by=user.id, parent=parent, kind=kind, siret=siret)
    except hierarchy.HierarchyError as e:
        db.rollback()
        raise HTTPException(status_code=e.status, detail=e.detail)
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

    audit.record(
        db,
        user_id=user.id,
        org_id=org.id,
        action="create",
        entity_type="organization",
        entity_id=org.id,
        meta={"slug": org.slug, "kind": org.kind, "parent_id": org.parent_id},
    )

    # Premier admin optionnel : pose ou crée l'user + son membership org_admin.
    first_admin_email = (first_admin_email or "").strip().lower()
    if first_admin_email:
        with SessionLocal() as bs:
            cu = load_or_provision_user(bs, first_admin_email, groups=[])
            try:
                tenancy.add_membership(bs, user_id=cu.id, org_id=org.id, role="org_admin")
            except Exception:
                pass  # déjà membre — idempotent
        audit.record(
            db,
            user_id=user.id,
            org_id=org.id,
            action="add_admin",
            entity_type="membership",
            entity_id=None,
            meta={"email": first_admin_email},
        )
    return RedirectResponse("/admin/organizations", status_code=303)

@router.get("/admin/users", response_class=HTMLResponse)
def admin_users(
    request: Request,
    user: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
    reset_link: str = "",
    reset_email: str = "",
    error: str = "",
):
    nav_org, nav_role = nav_fallback(db, user)
    return render(
        request, "admin_users.html", active="admin",
        org=nav_org, role=nav_role,
        users=accounts.list_users(db),
        reset_link=reset_link, reset_email=reset_email, error=error,
    )


@router.post("/admin/users/{user_id}/platform-admin")
def admin_user_toggle_admin(
    user_id: int,
    user: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
    value: str = Form(...),
):
    try:
        target = accounts.set_platform_admin(
            db, user_id=user_id, value=value == "1", acting_user_id=user.id
        )
    except ValueError as e:
        return RedirectResponse(f"/admin/users?error={quote(str(e))}", status_code=303)
    audit.record(
        db, user_id=user.id, org_id=None,
        action="promote_admin" if value == "1" else "demote_admin",
        entity_type="user", entity_id=target.id, meta={"email": target.email},
    )
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/reset-link")
def admin_user_reset_link(
    user_id: int,
    request: Request,
    user: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable.")
    tok = accounts.create_set_password_token(db, user_id=target.id)
    audit.record(
        db, user_id=user.id, org_id=None, action="password_reset_link",
        entity_type="user", entity_id=target.id, meta={"email": target.email},
    )
    link = f"{request.url.scheme}://{request.url.netloc}/reset/{tok.token}"
    return RedirectResponse(
        f"/admin/users?reset_link={quote(link)}&reset_email={quote(target.email)}",
        status_code=303,
    )


@router.get("/admin/audit", response_class=HTMLResponse)
def admin_audit_view(
    request: Request,
    user: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
    page: int = 0,
):
    PAGE_SIZE = 50
    rows = db.execute(
        select(AuditLog, User.email, Organization.slug)
        .outerjoin(User, User.id == AuditLog.user_id)
        .outerjoin(Organization, Organization.id == AuditLog.org_id)
        .order_by(AuditLog.at.desc())
        .offset(page * PAGE_SIZE)
        .limit(PAGE_SIZE)
    ).all()
    entries = [
        dict(
            id=al.id, at=al.at, action=al.action, entity_type=al.entity_type,
            entity_id=al.entity_id, meta=al.meta_json, actor=email or "—",
            org_slug=slug or "—",
        )
        for al, email, slug in rows
    ]
    nav_org, nav_role = nav_fallback(db, user)
    return render(
        request, "admin_audit.html", active="admin",
        org=nav_org, role=nav_role,
        entries=entries, page=page,
        next_page=page + 1 if len(entries) == PAGE_SIZE else None,
    )

@router.get("/admin/pricing", response_class=HTMLResponse)
def admin_pricing_view(
    request: Request,
    user: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    entries = pricing.list_pricing(db)
    nav_org, nav_role = nav_fallback(db, user)
    return render(
        request, "admin_pricing.html", active="admin",
        org=nav_org, role=nav_role, entries=entries,
    )


@router.post("/admin/pricing/{model_id}")
def admin_pricing_set(
    model_id: int,
    user: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
    input_price: str = Form(...),
    output_price: str = Form(...),
):
    try:
        ip = Decimal(input_price.replace(",", "."))
        op = Decimal(output_price.replace(",", "."))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Prix invalide : {e}")
    pricing.set_pricing(
        db, model_id=model_id, input_eur_per_1m=ip, output_eur_per_1m=op,
    )
    audit.record(
        db,
        user_id=user.id, org_id=None, action="set_pricing",
        entity_type="model_pricing", entity_id=model_id,
        meta={"input": str(ip), "output": str(op)},
    )
    return RedirectResponse("/admin/pricing", status_code=303)


_OR_IMPORT_MESSAGES: dict[str, tuple[str, str]] = {
    "created": ("success", "Modèle créé au catalogue GEOeval (prix importé si disponible)."),
    "created_unpriced": ("success", "Modèle créé au catalogue GEOeval — prix indisponible côté OpenRouter, à saisir dans « Tarifs des IA »."),
    "exists": ("info", "Ce modèle existe déjà au catalogue GEOeval — rien à créer."),
    "price_updated": ("success", "Prix importé : nouvelle version de tarif créée (l'ancienne est clôturée)."),
    "price_same": ("info", "Prix identique à la version en vigueur — aucune nouvelle version créée."),
    "not_found": ("error", "Modèle introuvable au catalogue GEOeval — crée-le d'abord."),
}


def _or_import_redirect(msg: str, q: str) -> RedirectResponse:
    params = urlencode({"load": 1, "q": q, "msg": msg})
    return RedirectResponse(f"/admin/openrouter-import?{params}", status_code=303)


@router.get("/admin/openrouter-import", response_class=HTMLResponse)
def admin_openrouter_import_view(
    request: Request,
    user: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
    load: int = 0,
    q: str = "",
    msg: str = "",
):
    """Écran d'import du catalogue OpenRouter — fetch à la demande uniquement."""
    entries: list[openrouter_catalog.CatalogEntry] = []
    existing: dict[str, object] = {}
    error: Optional[str] = None
    total = 0
    if load:
        try:
            entries = openrouter_catalog.fetch_catalog()
        except openrouter_catalog.CatalogError as exc:
            error = str(exc)
        total = len(entries)
        query = q.strip().lower()
        if query:
            entries = [
                e for e in entries
                if query in e.id.lower() or query in e.name.lower()
            ]
        existing = openrouter_catalog.existing_openrouter_models(db)
    kind, text = _OR_IMPORT_MESSAGES.get(msg, ("", ""))
    nav_org, nav_role = nav_fallback(db, user)
    return render(
        request, "admin_openrouter_import.html", active="admin",
        org=nav_org, role=nav_role,
        loaded=bool(load), entries=entries, existing=existing,
        total=total, q=q, error=error,
        msg_kind=kind, msg_text=text,
        rate=openrouter_catalog.usd_eur_rate(),
    )


@router.post("/admin/openrouter-import/create")
def admin_openrouter_import_create(
    user: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
    model_ref: str = Form(...),
    prompt_usd: str = Form(""),
    completion_usd: str = Form(""),
    q: str = Form(""),
):
    """Crée en 1 clic la ligne `models` (S5.1) + importe le prix (S5.2)."""
    model_ref = model_ref.strip()
    if not model_ref:
        raise HTTPException(status_code=400, detail="Identifiant OpenRouter manquant.")
    if openrouter_catalog.get_openrouter_model(db, model_ref) is not None:
        return _or_import_redirect("exists", q)  # idempotent
    model = services.create_model(
        db,
        model_name="openrouter",
        model_version=model_ref,
        base_url=None,          # défaut famille openrouter (llm_clients)
        api_key=None,           # clé plateforme via env
        extra_headers=None,
        search_config=None,     # à configurer ensuite par l'admin (ADR-080 §2.2)
    )
    priced = openrouter_catalog.import_pricing(
        db,
        model_id=model.model_id,
        prompt_usd_per_token=openrouter_catalog.parse_price(prompt_usd),
        completion_usd_per_token=openrouter_catalog.parse_price(completion_usd),
    )
    audit.record(
        db,
        user_id=user.id, org_id=None, action="import",
        entity_type="model", entity_id=model.model_id,
        meta={"source": "openrouter", "model_version": model_ref, "pricing_imported": priced},
    )
    return _or_import_redirect("created" if priced else "created_unpriced", q)


@router.post("/admin/openrouter-import/pricing")
def admin_openrouter_import_pricing(
    user: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
    model_ref: str = Form(...),
    prompt_usd: str = Form(""),
    completion_usd: str = Form(""),
    q: str = Form(""),
):
    """Importe le prix OpenRouter d'un modèle déjà présent (S5.2, versionné)."""
    model = openrouter_catalog.get_openrouter_model(db, model_ref.strip())
    if model is None:
        return _or_import_redirect("not_found", q)
    changed = openrouter_catalog.import_pricing(
        db,
        model_id=model.model_id,
        prompt_usd_per_token=openrouter_catalog.parse_price(prompt_usd),
        completion_usd_per_token=openrouter_catalog.parse_price(completion_usd),
    )
    if changed:
        audit.record(
            db,
            user_id=user.id, org_id=None, action="set_pricing",
            entity_type="model_pricing", entity_id=model.model_id,
            meta={"source": "openrouter", "model_version": model.model_version},
        )
    return _or_import_redirect("price_updated" if changed else "price_same", q)




@router.get("/admin/gold-annotations/import", response_class=HTMLResponse)
def gold_import_form(
    request: Request,
    user: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    nav_org, nav_role = nav_fallback(db, user)
    return render(
        request, "admin_gold_import.html", active="admin",
        org=nav_org, role=nav_role, result=None,
    )


@router.post("/admin/gold-annotations/import", response_class=HTMLResponse)
async def gold_import_submit(
    request: Request,
    user: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
    file: UploadFile = File(...),
):
    content = await file.read()
    result = gold.import_csv(db, content)
    audit.record(
        db, user_id=user.id, org_id=None,
        action="import_csv", entity_type="gold_annotations",
        entity_id=None,
        meta={"inserted": result["inserted"], "rejected": len(result["rejected"])},
    )
    nav_org, nav_role = nav_fallback(db, user)
    return render(
        request, "admin_gold_import.html", active="admin",
        org=nav_org, role=nav_role, result=result,
    )
