"""Pools de questions partagés par référence (E4, ADR-089 §2.5).

Router UI mince : les règles (visibilité, propriété, cycles) vivent dans
`geoeval.web.pools`. Lecture : tout membre de l'entité voit les pools qui lui
sont visibles. Écriture : éditeur+ de l'entité propriétaire.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from geoeval.web import audit, pools, services
from geoeval.web.auth import CurrentUser
from geoeval.web.deps import get_db, require_org, require_role, require_user
from geoeval.web.rendering import render
from geoeval.web.tenancy import role_at_least

router = APIRouter()


def _owned_or_http(db: Session, org, pool_id: int):
    try:
        return pools.get_owned(db, org, pool_id)
    except pools.PoolError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)


@router.get("/o/{org_slug}/pools", response_class=HTMLResponse)
def pools_list(request: Request, ctx=Depends(require_org), db: Session = Depends(get_db)):
    org, role = ctx
    rows = pools.list_visible(db, org)
    counts = {p.id: len(pools.resolve_pool_tests(db, p.id, org).test_ids) for p, _ in rows}
    usage = {p.id: pools.usage(db, p) for p, o in rows if o.id == org.id}
    return render(
        request, "pools.html", active="pools", org=org, role=role,
        own_pools=[(p, o) for p, o in rows if o.id == org.id],
        shared_pools=[(p, o) for p, o in rows if o.id != org.id],
        counts=counts, usage=usage,
        visibility_labels=pools.VISIBILITY_LABELS,
    )


@router.post("/o/{org_slug}/pools/new")
def pool_create(
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
    name: str = Form(...),
    description: str = Form(""),
    visibility: str = Form("private"),
):
    org, _ = ctx
    try:
        pool = pools.create(db, org, name=name, description=description, visibility=visibility, created_by=user.id)
    except pools.PoolError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    audit.record(
        db, user_id=user.id, org_id=org.id, action="create", entity_type="question_pool",
        entity_id=pool.id, meta={"name": pool.name, "visibility": pool.visibility},
    )
    return RedirectResponse(f"/o/{org.slug}/pools/{pool.id}", status_code=303)


@router.get("/o/{org_slug}/pools/{pool_id}", response_class=HTMLResponse)
def pool_detail(pool_id: int, request: Request, ctx=Depends(require_org), db: Session = Depends(get_db)):
    org, role = ctx
    try:
        pool, owner = pools.get_visible(db, org, pool_id)
    except pools.PoolError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    owned = owner.id == org.id
    can_edit = owned and role_at_least(role, "editor")
    direct_ids = pools.pool_test_ids(db, pool.id)
    resolution = pools.resolve_pool_tests(db, pool.id, org)
    direct = pools.tests_by_ids(db, direct_ids)
    inherited = pools.tests_by_ids(db, resolution.test_ids - set(direct_ids))
    included = []
    for cid in pools.included_ids(db, pool.id):
        child = pools.get(db, cid)
        if child is not None:
            c_owner = pools.owner_of(db, child)
            included.append((child, c_owner, pools.visible_to(child, c_owner, org)))
    addable, includable = [], []
    if can_edit:
        in_pool = set(direct_ids)
        addable = [t for t in services.list_tests(db, org.id) if t.test_id not in in_pool]
        already = {c.id for c, _, _ in included}
        includable = [(p, o) for p, o in pools.list_visible(db, org) if p.id != pool.id and p.id not in already]
    return render(
        request, "pool_detail.html", active="pools", org=org, role=role,
        pool=pool, owner=owner, owned=owned, can_edit=can_edit,
        direct_tests=direct, inherited_tests=inherited, origins=resolution.origins,
        included=included, addable_tests=addable, includable_pools=includable,
        usage=pools.usage(db, pool) if owned else None,
        visibility_labels=pools.VISIBILITY_LABELS,
    )


@router.post("/o/{org_slug}/pools/{pool_id}/edit")
def pool_edit(
    pool_id: int,
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
    name: str = Form(...),
    description: str = Form(""),
    visibility: str = Form("private"),
):
    org, _ = ctx
    pool = _owned_or_http(db, org, pool_id)
    try:
        pools.update(db, pool, name=name, description=description, set_description=True, visibility=visibility)
    except pools.PoolError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    audit.record(
        db, user_id=user.id, org_id=org.id, action="update", entity_type="question_pool",
        entity_id=pool.id, meta={"name": pool.name, "visibility": pool.visibility},
    )
    return RedirectResponse(f"/o/{org.slug}/pools/{pool.id}", status_code=303)


@router.post("/o/{org_slug}/pools/{pool_id}/delete")
def pool_delete(
    pool_id: int,
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
):
    org, _ = ctx
    pool = _owned_or_http(db, org, pool_id)
    name = pool.name
    counts = pools.delete(db, pool)
    audit.record(
        db, user_id=user.id, org_id=org.id, action="delete", entity_type="question_pool",
        entity_id=pool_id, meta={"name": name, **counts},
    )
    return RedirectResponse(f"/o/{org.slug}/pools", status_code=303)


@router.post("/o/{org_slug}/pools/{pool_id}/tests")
def pool_add_tests(
    pool_id: int,
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
    test_ids: list[int] = Form(default=[]),
):
    org, _ = ctx
    pool = _owned_or_http(db, org, pool_id)
    try:
        pools.add_tests(db, pool, test_ids)
    except pools.PoolError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    return RedirectResponse(f"/o/{org.slug}/pools/{pool.id}", status_code=303)


@router.post("/o/{org_slug}/pools/{pool_id}/tests/{test_id}/remove")
def pool_remove_test(
    pool_id: int, test_id: int,
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
):
    org, _ = ctx
    pool = _owned_or_http(db, org, pool_id)
    pools.remove_test(db, pool, test_id)
    return RedirectResponse(f"/o/{org.slug}/pools/{pool.id}", status_code=303)


@router.post("/o/{org_slug}/pools/{pool_id}/includes")
def pool_include(
    pool_id: int,
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
    child_pool_id: int = Form(...),
):
    org, _ = ctx
    pool = _owned_or_http(db, org, pool_id)
    child = pools.get(db, child_pool_id)
    if child is None:
        raise HTTPException(status_code=404, detail="Pool introuvable.")
    try:
        pools.include(db, pool, child)
    except pools.PoolError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    return RedirectResponse(f"/o/{org.slug}/pools/{pool.id}", status_code=303)


@router.post("/o/{org_slug}/pools/{pool_id}/includes/{child_pool_id}/remove")
def pool_exclude(
    pool_id: int, child_pool_id: int,
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
):
    org, _ = ctx
    pool = _owned_or_http(db, org, pool_id)
    pools.exclude(db, pool, child_pool_id)
    return RedirectResponse(f"/o/{org.slug}/pools/{pool.id}", status_code=303)
