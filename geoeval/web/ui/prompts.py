"""Prompts d'évaluation (catalogue global, édition admin plateforme).

Router UI (lot 1.3a) : découpage mécanique de l'ancien app.py, sans changement
de comportement. Les règles métier vivent dans les services (geoeval.web.*).
"""
from __future__ import annotations


from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from geoeval.web import (
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


@router.get("/o/{org_slug}/prompts", response_class=HTMLResponse)
def prompts(request: Request, ctx=Depends(require_org), db: Session = Depends(get_db)):
    org, role = ctx
    return render(
        request, "prompts.html", active="prompts", org=org, role=role,
        prompts=services.list_prompts(db),
    )


@router.get("/o/{org_slug}/prompts/new", response_class=HTMLResponse)
def prompt_new(
    request: Request,
    ctx=Depends(require_org),
    _pa: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    org, role = ctx
    return render(
        request,
        "prompt_form.html",
        active="prompts",
        org=org,
        role=role,
        prompt=None,
        prompt_types=services.list_prompt_types(db),
    )


@router.post("/o/{org_slug}/prompts/new")
def prompt_create(
    ctx=Depends(require_org),
    _pa: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
    prompt_type_id: int = Form(...),
    prompt_name: str = Form(...),
    prompt_text: str = Form(...),
):
    org, _ = ctx
    services.create_prompt(
        db, prompt_type_id=prompt_type_id, prompt_name=prompt_name, prompt_text=prompt_text
    )
    return RedirectResponse(f"/o/{org.slug}/prompts", status_code=303)


@router.get("/o/{org_slug}/prompts/{prompt_id}/edit", response_class=HTMLResponse)
def prompt_edit(
    prompt_id: int,
    request: Request,
    ctx=Depends(require_org),
    _pa: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    org, role = ctx
    prompt = services.get_prompt(db, prompt_id)
    if prompt is None:
        raise HTTPException(status_code=404, detail=f"Prompt {prompt_id} introuvable")
    return render(
        request,
        "prompt_form.html",
        active="prompts",
        org=org,
        role=role,
        prompt=prompt,
        prompt_types=services.list_prompt_types(db),
    )


@router.post("/o/{org_slug}/prompts/{prompt_id}/edit")
def prompt_update(
    prompt_id: int,
    ctx=Depends(require_org),
    _pa: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
    prompt_type_id: int = Form(...),
    prompt_name: str = Form(...),
    prompt_text: str = Form(...),
):
    org, _ = ctx
    services.update_prompt(
        db,
        prompt_id,
        prompt_type_id=prompt_type_id,
        prompt_name=prompt_name,
        prompt_text=prompt_text,
    )
    return RedirectResponse(f"/o/{org.slug}/prompts", status_code=303)
