"""Contrats LLM et politique de routage d'une entité (E5, ADR-089 §2.6).

Router UI mince, réservé aux org_admin (rôle hérité compris) : les règles vivent
dans `geoeval.web.contracts` et `geoeval.web.routing`. Les clés ne sont jamais
réaffichées : on peut seulement les remplacer ou les effacer.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from geoeval.web import audit, contracts, routing, services
from geoeval.web.auth import CurrentUser
from geoeval.web.deps import get_db, require_role, require_user
from geoeval.web.rendering import render

router = APIRouter()


def _http(exc: contracts.ContractError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


def _form_ctx(db: Session) -> dict:
    models = services.list_models(db, active_only=False)
    from geoeval.core.llm_clients import provider_family

    return dict(
        families=contracts.FAMILIES, family_labels=contracts.FAMILY_LABELS,
        hostings=contracts.HOSTINGS, hosting_labels=contracts.HOSTING_LABELS,
        models_by_family={f: [m for m in models if provider_family(m.model_name) == f] for f in contracts.FAMILIES},
    )


@router.get("/o/{org_slug}/contracts", response_class=HTMLResponse)
def contracts_page(request: Request, ctx=Depends(require_role("org_admin")), db: Session = Depends(get_db)):
    org, role = ctx
    own = contracts.list_own(db, org.id)
    inherited = contracts.list_inherited(db, org)
    used = contracts.spent_by_contract(db, [c.id for c in own] + [c.id for c, _ in inherited])
    # Clé effectivement utilisée pour chaque IA active (contrat, plateforme ou blocage).
    effective = []
    for m in services.list_models(db):
        effective.append((m, contracts.resolve(db, org, m)))
    return render(
        request, "contracts.html", active="settings", org=org, role=role,
        own=own, inherited=inherited, used=used, effective=effective,
        statuses={c.id: contracts.status(c, used.get(c.id, 0)) for c in own + [c for c, _ in inherited]},
        status_labels=contracts.STATUS_LABELS, window_label=contracts.window_label,
        policy=routing.get_own(db, org.id), effective_policy=routing.effective(db, org),
        **_form_ctx(db),
    )


@router.post("/o/{org_slug}/routing-policy")
def routing_policy_submit(
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
    restrict_families: bool = Form(False),
    allowed_families: list[str] = Form(default=[]),
    sovereign_only: bool = Form(False),
    eu_only: bool = Form(False),
):
    org, _ = ctx
    try:
        routing.set_policy(
            db, org, allowed_families=allowed_families if restrict_families else None,
            sovereign_only=sovereign_only, eu_only=eu_only, updated_by=user.id,
        )
    except contracts.ContractError as e:
        raise _http(e)
    audit.record(db, user_id=user.id, org_id=org.id, action="update", entity_type="routing_policy", entity_id=org.id,
                 meta={"allowed_families": allowed_families if restrict_families else None,
                       "sovereign_only": sovereign_only, "eu_only": eu_only})
    return RedirectResponse(f"/o/{org.slug}/contracts", status_code=303)


@router.get("/o/{org_slug}/contracts/new", response_class=HTMLResponse)
def contract_new_form(request: Request, ctx=Depends(require_role("org_admin")), db: Session = Depends(get_db)):
    org, role = ctx
    return render(request, "contract_form.html", active="settings", org=org, role=role, contract=None, **_form_ctx(db))


def _fields(label, reference, base_url, extra_headers, model_ids, valid_from, valid_to, cap_eur, hosting,
            sovereign, is_active) -> dict:
    return dict(label=label, reference=reference, base_url=base_url, extra_headers=extra_headers,
                model_ids=model_ids, valid_from=valid_from, valid_to=valid_to, cap_eur=cap_eur,
                hosting=hosting, sovereign=sovereign, is_active=is_active)


@router.post("/o/{org_slug}/contracts/new")
def contract_create(
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
    family: str = Form(...),
    label: str = Form(...),
    reference: str = Form(""),
    base_url: str = Form(""),
    api_key: str = Form(""),
    extra_headers: str = Form(""),
    model_ids: list[int] = Form(default=[]),
    valid_from: str = Form(""),
    valid_to: str = Form(""),
    cap_eur: str = Form(""),
    hosting: str = Form(""),
    sovereign: bool = Form(False),
    is_active: bool = Form(False),
):
    org, _ = ctx
    try:
        c = contracts.create(
            db, org, family=family, api_key=api_key.strip() or None, created_by=user.id,
            **_fields(label, reference, base_url, extra_headers, model_ids, valid_from, valid_to, cap_eur,
                      hosting, sovereign, is_active),
        )
    except contracts.ContractError as e:
        raise _http(e)
    audit.record(db, user_id=user.id, org_id=org.id, action="create", entity_type="llm_contract", entity_id=c.id,
                 meta={"family": c.family, "label": c.label})
    return RedirectResponse(f"/o/{org.slug}/contracts", status_code=303)


@router.get("/o/{org_slug}/contracts/{contract_id}/edit", response_class=HTMLResponse)
def contract_edit_form(contract_id: int, request: Request, ctx=Depends(require_role("org_admin")),
                       db: Session = Depends(get_db)):
    org, role = ctx
    try:
        c = contracts.get_owned(db, org, contract_id)
    except contracts.ContractError as e:
        raise _http(e)
    return render(request, "contract_form.html", active="settings", org=org, role=role, contract=c,
                  used=contracts.spent(db, c.id), **_form_ctx(db))


@router.post("/o/{org_slug}/contracts/{contract_id}/edit")
def contract_edit_submit(
    contract_id: int,
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
    label: str = Form(...),
    reference: str = Form(""),
    base_url: str = Form(""),
    api_key: str = Form(""),
    clear_api_key: bool = Form(False),
    extra_headers: str = Form(""),
    model_ids: list[int] = Form(default=[]),
    valid_from: str = Form(""),
    valid_to: str = Form(""),
    cap_eur: str = Form(""),
    hosting: str = Form(""),
    sovereign: bool = Form(False),
    is_active: bool = Form(False),
):
    org, _ = ctx
    try:
        c = contracts.get_owned(db, org, contract_id)
        contracts.update(
            db, c, api_key=api_key.strip() or None, clear_api_key=clear_api_key,
            **_fields(label, reference, base_url, extra_headers, model_ids, valid_from, valid_to, cap_eur,
                      hosting, sovereign, is_active),
        )
    except contracts.ContractError as e:
        raise _http(e)
    audit.record(db, user_id=user.id, org_id=org.id, action="update", entity_type="llm_contract", entity_id=c.id,
                 meta={"label": c.label, "key_changed": bool(api_key.strip()) or clear_api_key})
    return RedirectResponse(f"/o/{org.slug}/contracts", status_code=303)


@router.post("/o/{org_slug}/contracts/{contract_id}/delete")
def contract_delete(
    contract_id: int,
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
):
    org, _ = ctx
    try:
        c = contracts.get_owned(db, org, contract_id)
        label: Optional[str] = c.label
        contracts.delete(db, c)
    except contracts.ContractError as e:
        raise _http(e)
    audit.record(db, user_id=user.id, org_id=org.id, action="delete", entity_type="llm_contract",
                 entity_id=contract_id, meta={"label": label})
    return RedirectResponse(f"/o/{org.slug}/contracts", status_code=303)
