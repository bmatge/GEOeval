"""Catalogue des modèles (admin plateforme). Les clés des entités sont des contrats (E5, ui/contracts.py).

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
    contracts,
    services,
)
from geoeval.web.auth import CurrentUser
from geoeval.web.deps import (
    get_db,
    require_org,
    require_platform_admin,
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
    return render(request, "models.html", active="models", org=org, role=role,
                  models=models, refs=refs, hosting_labels=contracts.HOSTING_LABELS)


@router.get("/o/{org_slug}/models/new", response_class=HTMLResponse)
def model_new_form(
    request: Request,
    ctx=Depends(require_org),
    _pa: CurrentUser = Depends(require_platform_admin),
):
    org, role = ctx
    return render(request, "model_form.html", active="models", org=org, role=role,
                  model=None, providers=PROVIDER_CHOICES, hostings=contracts.HOSTINGS,
                  hosting_labels=contracts.HOSTING_LABELS)


def _parse_hosting(raw: str) -> Optional[str]:
    raw = (raw or "").strip()
    if raw and raw not in contracts.HOSTINGS:
        raise HTTPException(status_code=400, detail=f"Hébergement invalide : {raw!r}")
    return raw or None


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
    hosting: str = Form(""),
    is_sovereign: bool = Form(False),
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
        hosting=_parse_hosting(hosting),
        is_sovereign=is_sovereign,
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
                  model=model, providers=PROVIDER_CHOICES, hostings=contracts.HOSTINGS,
                  hosting_labels=contracts.HOSTING_LABELS)


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
    hosting: str = Form(""),
    is_sovereign: bool = Form(False),
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
        hosting=_parse_hosting(hosting),
        is_sovereign=is_sovereign,
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
