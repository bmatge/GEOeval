"""Périmètres (sites) : objet intermédiaire organisation → questions.

Router UI (lot 1.3a) : découpage mécanique de l'ancien app.py, sans changement
de comportement. Les règles métier vivent dans les services (geoeval.web.*).
"""
from __future__ import annotations


from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from geoeval.web import (
    audit,
    perimeters,
    pools,
    services,
    themes,
)
from geoeval.web.auth import CurrentUser
from geoeval.web.deps import (
    get_db,
    require_org,
    require_role,
    require_user,
)
from geoeval.web.rendering import render

router = APIRouter()


@router.get("/o/{org_slug}/perimeters", response_class=HTMLResponse)
def perimeters_list(request: Request, ctx=Depends(require_org), db: Session = Depends(get_db)):
    org, role = ctx
    plist = perimeters.list_for_org(db, org.id)
    counts = {p.id: perimeters.count_tests(db, p.id) for p in plist}
    return render(
        request, "perimeters.html", active="perimeters", org=org, role=role,
        perimeters=plist, counts=counts,
    )


@router.get("/o/{org_slug}/perimeters/new", response_class=HTMLResponse)
def perimeter_new_form(
    request: Request, ctx=Depends(require_role("editor")), db: Session = Depends(get_db)
):
    org, role = ctx
    return render(
        request, "perimeter_form.html", active="perimeters", org=org, role=role,
        perimeter=None, all_themes=themes.list_all(db), selected_theme_ids=set(),
    )


@router.post("/o/{org_slug}/perimeters/new")
def perimeter_create(
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
    name: str = Form(...),
    slug: str = Form(...),
    kind: str = Form(""),
    home_url: str = Form(""),
    description: str = Form(""),
    domains: str = Form(""),
    theme_ids: list[int] = Form(default=[]),
):
    org, _ = ctx
    try:
        theme_ids = themes.validate_ids(db, theme_ids)
        p = perimeters.create(
            db, org_id=org.id,
            name=name, slug=slug, kind=kind or None,
            home_url=home_url or None, description=description or None,
            created_by=user.id, domains=domains,
        )
        themes.set_for_perimeter(db, p.id, theme_ids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit.record(
        db, user_id=user.id, org_id=org.id,
        action="create", entity_type="perimeter", entity_id=p.id,
        meta={"slug": p.slug, "name": p.name},
    )
    return RedirectResponse(f"/o/{org.slug}/perimeters/{p.id}", status_code=303)


@router.get("/o/{org_slug}/perimeters/{perimeter_id}", response_class=HTMLResponse)
def perimeter_detail(
    perimeter_id: int, request: Request,
    ctx=Depends(require_org), db: Session = Depends(get_db),
):
    org, role = ctx
    p = perimeters.get_by_id(db, org.id, perimeter_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Périmètre introuvable.")
    # Questions des AUTRES périmètres, proposables au rattachement.
    attachable = [t for t in services.list_tests(db, org.id) if t.perimeter_id != p.id]
    # E4 : questions apportées par les pools abonnés (lecture seule ici).
    resolution = pools.effective_resolution(db, p)
    pooled = [
        t for t in pools.effective_tests(db, p, active_only=False, ready_only=False)
        if t.perimeter_id != p.id
    ]
    subs = pools.subscriptions(db, p)
    subscribed_ids = {s.pool.id for s in subs}
    subscribable = [(pl, ow) for pl, ow in pools.list_visible(db, org) if pl.id not in subscribed_ids]
    own_tests = services.list_tests(db, org.id, perimeter_id=p.id)
    return render(
        request, "perimeter_detail.html", active="perimeters", org=org, role=role,
        perimeter=p,
        tests=own_tests,
        all_perimeters=perimeters.list_for_org(db, org.id),
        attachable_tests=attachable,
        perimeter_themes=themes.for_perimeter(db, p.id),
        test_themes=themes.for_tests(db, [t.test_id for t in own_tests + pooled]),
        subscriptions=subs,
        subscribable_pools=subscribable,
        pooled_tests=pooled,
        pooled_origins=resolution.origins,
    )


@router.post("/o/{org_slug}/perimeters/{perimeter_id}/pools")
def perimeter_subscribe_pool(
    perimeter_id: int,
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
    pool_id: int = Form(...),
):
    """Abonne un pool visible à ce périmètre (E4) : ses questions rejoignent les runs."""
    org, _ = ctx
    p = perimeters.get_by_id(db, org.id, perimeter_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Périmètre introuvable.")
    try:
        pool = pools.subscribe(db, p, pool_id, created_by=user.id)
    except pools.PoolError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    audit.record(
        db, user_id=user.id, org_id=org.id,
        action="subscribe_pool", entity_type="perimeter", entity_id=p.id,
        meta={"pool_id": pool.id, "pool": pool.name},
    )
    return RedirectResponse(f"/o/{org.slug}/perimeters/{perimeter_id}", status_code=303)


@router.post("/o/{org_slug}/perimeters/{perimeter_id}/pools/{pool_id}/unsubscribe")
def perimeter_unsubscribe_pool(
    perimeter_id: int,
    pool_id: int,
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
):
    org, _ = ctx
    p = perimeters.get_by_id(db, org.id, perimeter_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Périmètre introuvable.")
    pools.unsubscribe(db, p, pool_id)
    audit.record(
        db, user_id=user.id, org_id=org.id,
        action="unsubscribe_pool", entity_type="perimeter", entity_id=p.id,
        meta={"pool_id": pool_id},
    )
    return RedirectResponse(f"/o/{org.slug}/perimeters/{perimeter_id}", status_code=303)


@router.post("/o/{org_slug}/perimeters/{perimeter_id}/attach-test")
def perimeter_attach_test(
    perimeter_id: int,
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
    test_id: int = Form(...),
):
    """Rattache une question existante (d'un autre périmètre) à ce périmètre."""
    org, _ = ctx
    p = perimeters.get_by_id(db, org.id, perimeter_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Périmètre introuvable.")
    test = services.get_test(db, org.id, test_id)
    if test is None:
        raise HTTPException(status_code=404, detail="Question introuvable.")
    if test.perimeter_id != perimeter_id:
        perimeters.move_test(db, org_id=org.id, test_id=test_id, to_perimeter_id=perimeter_id)
        audit.record(
            db, user_id=user.id, org_id=org.id,
            action="move_test", entity_type="test", entity_id=test_id,
            meta={"to_perimeter_id": perimeter_id},
        )
    return RedirectResponse(f"/o/{org.slug}/perimeters/{perimeter_id}", status_code=303)


@router.get("/o/{org_slug}/perimeters/{perimeter_id}/edit", response_class=HTMLResponse)
def perimeter_edit_form(
    perimeter_id: int, request: Request,
    ctx=Depends(require_role("editor")), db: Session = Depends(get_db),
):
    org, role = ctx
    p = perimeters.get_by_id(db, org.id, perimeter_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Périmètre introuvable.")
    return render(
        request, "perimeter_form.html", active="perimeters", org=org, role=role,
        perimeter=p, all_themes=themes.list_all(db),
        selected_theme_ids={t.id for t in themes.for_perimeter(db, p.id)},
    )


@router.post("/o/{org_slug}/perimeters/{perimeter_id}/edit")
def perimeter_edit_submit(
    perimeter_id: int,
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
    name: str = Form(...),
    kind: str = Form(""),
    home_url: str = Form(""),
    description: str = Form(""),
    domains: str = Form(""),
    theme_ids: list[int] = Form(default=[]),
):
    org, _ = ctx
    try:
        theme_ids = themes.validate_ids(db, theme_ids)
        perimeters.update(
            db, org_id=org.id, perimeter_id=perimeter_id,
            name=name, kind=kind or None,
            home_url=home_url or None, description=description or None,
            domains=domains, set_domains=True,
        )
        themes.set_for_perimeter(db, perimeter_id, theme_ids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit.record(
        db, user_id=user.id, org_id=org.id,
        action="update", entity_type="perimeter", entity_id=perimeter_id,
    )
    return RedirectResponse(f"/o/{org.slug}/perimeters/{perimeter_id}", status_code=303)


@router.post("/o/{org_slug}/perimeters/{perimeter_id}/delete")
def perimeter_delete(
    perimeter_id: int,
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
):
    org, _ = ctx
    try:
        perimeters.delete(db, org.id, perimeter_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit.record(
        db, user_id=user.id, org_id=org.id,
        action="delete", entity_type="perimeter", entity_id=perimeter_id,
    )
    return RedirectResponse(f"/o/{org.slug}/perimeters", status_code=303)
