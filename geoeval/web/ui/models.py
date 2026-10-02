"""Catalogue des modèles (admin plateforme) et clés BYOK par organisation (org_admin).

Router UI (lot 1.3a) : découpage mécanique de l'ancien app.py, sans changement
de comportement. Les règles métier vivent dans les services (geoeval.web.*).
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from geoeval.web import (
    audit,
    credentials,
    services,
)
from geoeval.web.auth import CurrentUser
from geoeval.web.deps import (
    get_db,
    require_org,
    require_platform_admin,
    require_role,
    require_user,
)
from geoeval.web.rendering import render

router = APIRouter()


PROVIDER_CHOICES = ["openrouter", "chatGPT", "mistral", "gemini", "albert", "compatible-openai"]

def _parse_headers(raw: str) -> Optional[dict]:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"En-têtes HTTP : JSON invalide ({exc}).")
    if not isinstance(obj, dict) or not all(isinstance(v, str) for v in obj.values()):
        raise HTTPException(
            status_code=400,
            detail='En-têtes HTTP : objet JSON attendu, valeurs texte (ex. {"X-Api-Version": "2"}).',
        )
    return obj


@router.get("/o/{org_slug}/models", response_class=HTMLResponse)
def models_list(request: Request, ctx=Depends(require_org), db: Session = Depends(get_db)):
    org, role = ctx
    models = services.list_models(db, active_only=False)
    refs = {m.model_id: services.model_run_refs(db, m.model_id) for m in models}
    creds = credentials.list_for_org(db, org.id)
    creds_by_model = {c.model_id: c for c in creds}
    return render(request, "models.html", active="models", org=org, role=role,
                  models=models, refs=refs, creds_by_model=creds_by_model)


# ---- BYOK : configuration par org (org_admin+) --------------------
@router.get("/o/{org_slug}/credentials/{model_id}/edit", response_class=HTMLResponse)
def credential_edit_form(
    model_id: int,
    request: Request,
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
):
    org, role = ctx
    model = services.get_model(db, model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="Modèle introuvable.")
    cred = credentials.get_for_model(db, org.id, model_id)
    return render(
        request, "credential_form.html", active="models", org=org, role=role,
        model=model, cred=cred,
    )


@router.post("/o/{org_slug}/credentials/{model_id}/edit")
def credential_edit_submit(
    model_id: int,
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
    base_url: str = Form(""),
    api_key: str = Form(""),
    clear_api_key: bool = Form(False),
    extra_headers: str = Form(""),
    is_active: bool = Form(False),
):
    org, _ = ctx
    model = services.get_model(db, model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="Modèle introuvable.")
    credentials.upsert(
        db,
        org_id=org.id,
        model_id=model_id,
        base_url=base_url.strip() or None,
        api_key=api_key.strip() or None,
        clear_api_key=clear_api_key,
        extra_headers=_parse_headers(extra_headers),
        is_active=is_active,
    )
    audit.record(
        db,
        user_id=user.id,
        org_id=org.id,
        action="upsert",
        entity_type="org_credential",
        entity_id=model_id,
        meta={"model_version": model.model_version, "clear": clear_api_key},
    )
    return RedirectResponse(f"/o/{org.slug}/models", status_code=303)


@router.post("/o/{org_slug}/credentials/{model_id}/delete")
def credential_delete(
    model_id: int,
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
):
    org, _ = ctx
    credentials.delete(db, org.id, model_id)
    audit.record(
        db,
        user_id=user.id,
        org_id=org.id,
        action="delete",
        entity_type="org_credential",
        entity_id=model_id,
    )
    return RedirectResponse(f"/o/{org.slug}/models", status_code=303)


@router.get("/o/{org_slug}/models/new", response_class=HTMLResponse)
def model_new_form(
    request: Request,
    ctx=Depends(require_org),
    _pa: CurrentUser = Depends(require_platform_admin),
):
    org, role = ctx
    return render(request, "model_form.html", active="models", org=org, role=role,
                  model=None, providers=PROVIDER_CHOICES)


def _parse_search_config(
    engine: str, max_results: str, context_size: str, allowed_domains: str
) -> Optional[dict]:
    """Assemble models.search_config depuis les champs du formulaire (ADR-080 §2.2).

    engine vide ou "off" sans autre champ → None (pas de recherche web).
    """
    engine = (engine or "").strip().lower()
    if engine not in {"", "off", "native", "exa", "firecrawl"}:
        raise HTTPException(status_code=400, detail=f"engine invalide : {engine!r}")
    config: dict = {}
    if engine and engine != "off":
        config["engine"] = engine
    if (max_results or "").strip():
        try:
            n = int(max_results)
            if not 1 <= n <= 20:
                raise ValueError
        except ValueError:
            raise HTTPException(status_code=400, detail="max_results doit être un entier entre 1 et 20")
        config["max_results"] = n
    context_size = (context_size or "").strip().lower()
    if context_size:
        if context_size not in {"low", "medium", "high"}:
            raise HTTPException(status_code=400, detail=f"search_context_size invalide : {context_size!r}")
        config["search_context_size"] = context_size
    domains = [d.strip() for d in (allowed_domains or "").split(",") if d.strip()]
    if domains:
        config["allowed_domains"] = domains
    if engine == "off":
        return {"engine": "off"}  # explicite : recherche coupée malgré d'autres champs
    return config or None


@router.post("/o/{org_slug}/models/new")
def model_new_submit(
    ctx=Depends(require_org),
    _pa: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
    model_name: str = Form(...),
    model_version: str = Form(...),
    base_url: str = Form(""),
    api_key: str = Form(""),
    extra_headers: str = Form(""),
    search_engine: str = Form(""),
    search_max_results: str = Form(""),
    search_context_size: str = Form(""),
    search_allowed_domains: str = Form(""),
):
    org, _ = ctx
    services.create_model(
        db,
        model_name=model_name.strip(),
        model_version=model_version.strip(),
        base_url=base_url.strip(),
        api_key=api_key.strip(),
        extra_headers=_parse_headers(extra_headers),
        search_config=_parse_search_config(
            search_engine, search_max_results, search_context_size, search_allowed_domains
        ),
    )
    return RedirectResponse(f"/o/{org.slug}/models", status_code=303)


@router.get("/o/{org_slug}/models/{model_id}/edit", response_class=HTMLResponse)
def model_edit_form(
    model_id: int,
    request: Request,
    ctx=Depends(require_org),
    _pa: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    org, role = ctx
    model = services.get_model(db, model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="Modèle introuvable.")
    return render(request, "model_form.html", active="models", org=org, role=role,
                  model=model, providers=PROVIDER_CHOICES)


@router.post("/o/{org_slug}/models/{model_id}/edit")
def model_edit_submit(
    model_id: int,
    ctx=Depends(require_org),
    _pa: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
    model_name: str = Form(...),
    model_version: str = Form(...),
    base_url: str = Form(""),
    api_key: str = Form(""),
    clear_api_key: bool = Form(False),
    extra_headers: str = Form(""),
    search_engine: str = Form(""),
    search_max_results: str = Form(""),
    search_context_size: str = Form(""),
    search_allowed_domains: str = Form(""),
):
    org, _ = ctx
    services.update_model(
        db,
        model_id,
        model_name=model_name.strip(),
        model_version=model_version.strip(),
        base_url=base_url.strip(),
        api_key=api_key.strip(),
        clear_api_key=clear_api_key,
        extra_headers=_parse_headers(extra_headers),
        search_config=_parse_search_config(
            search_engine, search_max_results, search_context_size, search_allowed_domains
        ),
    )
    return RedirectResponse(f"/o/{org.slug}/models", status_code=303)


@router.post("/o/{org_slug}/models/{model_id}/toggle-judge")
def model_toggle_judge(
    model_id: int,
    ctx=Depends(require_org),
    _pa: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    org, _ = ctx
    services.toggle_model_judge(db, model_id)
    return RedirectResponse(f"/o/{org.slug}/models", status_code=303)


@router.post("/o/{org_slug}/models/{model_id}/deactivate")
def model_deactivate(
    model_id: int,
    ctx=Depends(require_org),
    _pa: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    org, _ = ctx
    services.set_model_active(db, model_id, False)
    return RedirectResponse(f"/o/{org.slug}/models", status_code=303)


@router.post("/o/{org_slug}/models/{model_id}/reactivate")
def model_reactivate(
    model_id: int,
    ctx=Depends(require_org),
    _pa: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    org, _ = ctx
    services.set_model_active(db, model_id, True)
    return RedirectResponse(f"/o/{org.slug}/models", status_code=303)


@router.post("/o/{org_slug}/models/{model_id}/delete")
def model_delete(
    model_id: int,
    ctx=Depends(require_org),
    _pa: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    org, _ = ctx
    try:
        services.delete_model(db, model_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return RedirectResponse(f"/o/{org.slug}/models", status_code=303)
