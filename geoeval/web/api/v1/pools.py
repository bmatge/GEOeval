"""Pools de questions partagés par référence (E4, ADR-089 §2.5).

Lecture : membres de l'entité, sur les pools qui lui sont visibles (404 sinon,
sans divulgation). Écriture : éditeur+ de l'entité propriétaire du pool.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from geoeval.web import audit, pools
from geoeval.web.api.deps import Principal, require_role
from geoeval.web.api.schemas import PoolIn, PoolOut, PoolPatch, PoolRefIn, PoolTestsIn
from geoeval.web.deps import get_db

router = APIRouter(prefix="/orgs/{org_slug}/pools", tags=["pools"])


def _http(exc: pools.PoolError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


def _out(db: Session, pool, owner, viewer) -> PoolOut:
    return PoolOut(
        id=pool.id, name=pool.name, description=pool.description, visibility=pool.visibility,
        owner_org_id=owner.id, owner_org_slug=owner.slug, created_at=pool.created_at,
        test_ids=pools.pool_test_ids(db, pool.id), included_pool_ids=pools.included_ids(db, pool.id),
        n_questions=len(pools.resolve_pool_tests(db, pool.id, viewer).test_ids),
    )


def _owned(db: Session, principal: Principal, pool_id: int):
    try:
        return pools.get_owned(db, principal.org, pool_id)
    except pools.PoolError as exc:
        raise _http(exc)


def _audit(db: Session, principal: Principal, action: str, pool_id: int, **meta) -> None:
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action=action, entity_type="question_pool",
                 entity_id=pool_id, meta={**meta, **principal.audit_meta()})


@router.get("", response_model=list[PoolOut], summary="Pools visibles de l'entité (les siens d'abord)")
def list_pools(principal: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db),
               owned_only: bool = False):
    rows = pools.list_visible(db, principal.org)
    if owned_only:
        rows = [(p, o) for p, o in rows if o.id == principal.org.id]
    return [_out(db, p, o, principal.org) for p, o in rows]


@router.post("", response_model=PoolOut, status_code=status.HTTP_201_CREATED, summary="Créer un pool (editor+)")
def create_pool(body: PoolIn, principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    try:
        pool = pools.create(db, principal.org, name=body.name, description=body.description,
                            visibility=body.visibility, created_by=principal.user_id)
    except pools.PoolError as exc:
        raise _http(exc)
    _audit(db, principal, "create", pool.id, name=pool.name, visibility=pool.visibility)
    return _out(db, pool, principal.org, principal.org)


@router.get("/{pool_id}", response_model=PoolOut, summary="Un pool visible")
def get_pool(pool_id: int, principal: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    try:
        pool, owner = pools.get_visible(db, principal.org, pool_id)
    except pools.PoolError as exc:
        raise _http(exc)
    return _out(db, pool, owner, principal.org)


@router.patch("/{pool_id}", response_model=PoolOut, summary="Modifier un pool (editor+ de l'entité propriétaire)")
def update_pool(pool_id: int, body: PoolPatch, principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    pool = _owned(db, principal, pool_id)
    fields = body.model_dump(exclude_unset=True)
    try:
        pools.update(db, pool, name=fields.get("name"), description=fields.get("description"),
                     set_description="description" in fields, visibility=fields.get("visibility"))
    except pools.PoolError as exc:
        raise _http(exc)
    _audit(db, principal, "update", pool.id, fields=sorted(fields))
    return _out(db, pool, principal.org, principal.org)


@router.delete("/{pool_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Supprimer un pool (editor+) — abonnements et inclusions disparaissent, les runs restent")
def delete_pool(pool_id: int, principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    pool = _owned(db, principal, pool_id)
    name = pool.name
    counts = pools.delete(db, pool)
    _audit(db, principal, "delete", pool_id, name=name, **counts)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{pool_id}/questions", response_model=PoolOut, summary="Ajouter des questions de l'entité au pool (editor+)")
def add_questions(pool_id: int, body: PoolTestsIn, principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    pool = _owned(db, principal, pool_id)
    try:
        added = pools.add_tests(db, pool, body.test_ids)
    except pools.PoolError as exc:
        raise _http(exc)
    _audit(db, principal, "add_tests", pool.id, added=added)
    return _out(db, pool, principal.org, principal.org)


@router.delete("/{pool_id}/questions/{test_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Retirer une question du pool (editor+)")
def remove_question(pool_id: int, test_id: int, principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    pool = _owned(db, principal, pool_id)
    pools.remove_test(db, pool, test_id)
    _audit(db, principal, "remove_test", pool.id, test_id=test_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{pool_id}/includes", response_model=PoolOut,
             summary="Inclure un autre pool visible (editor+) — 409 si cela crée un cycle")
def include_pool(pool_id: int, body: PoolRefIn, principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    pool = _owned(db, principal, pool_id)
    child = pools.get(db, body.pool_id)
    if child is None:
        raise HTTPException(status_code=404, detail="Pool introuvable.")
    try:
        pools.include(db, pool, child)
    except pools.PoolError as exc:
        raise _http(exc)
    _audit(db, principal, "include", pool.id, child_pool_id=child.id)
    return _out(db, pool, principal.org, principal.org)


@router.delete("/{pool_id}/includes/{child_pool_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Retirer une inclusion (editor+)")
def exclude_pool(pool_id: int, child_pool_id: int, principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    pool = _owned(db, principal, pool_id)
    pools.exclude(db, pool, child_pool_id)
    _audit(db, principal, "exclude", pool.id, child_pool_id=child_pool_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
