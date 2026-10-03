"""Périmètres (sites) d'une organisation — lecture, membres ; abonnements aux pools (E4)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from geoeval.web import audit, pools, themes
from geoeval.web import perimeters as perimeters_svc
from geoeval.web.api.deps import Principal, require_role
from geoeval.web.api.schemas import (
    EffectiveQuestionOut,
    PerimeterIn,
    PerimeterOut,
    PerimeterPatch,
    PoolRefIn,
    SubscriptionOut,
)
from geoeval.web.deps import get_db

router = APIRouter(prefix="/orgs/{org_slug}/perimeters", tags=["périmètres"])


def _out(db: Session, p) -> PerimeterOut:
    out = PerimeterOut.model_validate(p)
    out.n_questions = perimeters_svc.count_tests(db, p.id)
    out.n_pooled_questions = len(pools.effective_resolution(db, p).test_ids)
    out.theme_ids = [t.id for t in themes.for_perimeter(db, p.id)]
    return out


def _get_or_404(db: Session, org_id: int, perimeter_id: int):
    p = perimeters_svc.get_by_id(db, org_id, perimeter_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Périmètre introuvable.")
    return p


@router.get("", response_model=list[PerimeterOut], summary="Périmètres de l'organisation")
def list_perimeters(principal: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    return [_out(db, p) for p in perimeters_svc.list_for_org(db, principal.org.id)]


@router.get("/{perimeter_id}", response_model=PerimeterOut, summary="Un périmètre")
def get_perimeter(perimeter_id: int, principal: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    p = perimeters_svc.get_by_id(db, principal.org.id, perimeter_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Périmètre introuvable.")
    return _out(db, p)


# ---- Écriture (lot 1.3c, editor+) -----------------------------------
def _value_error(exc: ValueError) -> HTTPException:
    msg = str(exc)
    if "introuvable" in msg:
        return HTTPException(status_code=404, detail=msg)
    if "existe déjà" in msg or "contient encore" in msg or "pas supprimable" in msg:
        return HTTPException(status_code=409, detail=msg)
    return HTTPException(status_code=400, detail=msg)


@router.post("", response_model=PerimeterOut, status_code=status.HTTP_201_CREATED, summary="Créer un périmètre (editor+)")
def create_perimeter(body: PerimeterIn, principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    try:
        theme_ids = themes.validate_ids(db, body.theme_ids)
        p = perimeters_svc.create(
            db, org_id=principal.org.id, name=body.name, slug=body.slug, kind=body.kind,
            home_url=body.home_url, description=body.description, created_by=principal.user_id,
            domains=body.domains,
        )
        themes.set_for_perimeter(db, p.id, theme_ids)
    except ValueError as exc:
        raise _value_error(exc)
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action="create", entity_type="perimeter",
                 entity_id=p.id, meta={"slug": p.slug, "name": p.name, **principal.audit_meta()})
    return _out(db, p)


@router.patch("/{perimeter_id}", response_model=PerimeterOut, summary="Modifier un périmètre (editor+) — champs optionnels")
def update_perimeter(perimeter_id: int, body: PerimeterPatch, principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    p = perimeters_svc.get_by_id(db, principal.org.id, perimeter_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Périmètre introuvable.")
    fields = body.model_dump(exclude_unset=True)
    try:
        theme_ids = themes.validate_ids(db, fields["theme_ids"] or []) if "theme_ids" in fields else None
        p = perimeters_svc.update(
            db, org_id=principal.org.id, perimeter_id=perimeter_id,
            name=fields.get("name", p.name), kind=fields.get("kind", p.kind),
            home_url=fields.get("home_url", p.home_url), description=fields.get("description", p.description),
            domains=fields.get("domains") or [], set_domains="domains" in fields,
        )
        if theme_ids is not None:
            themes.set_for_perimeter(db, perimeter_id, theme_ids)
    except ValueError as exc:
        raise _value_error(exc)
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action="update", entity_type="perimeter",
                 entity_id=perimeter_id, meta={"fields": sorted(fields), **principal.audit_meta()})
    return _out(db, p)


@router.delete("/{perimeter_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Supprimer un périmètre vide (editor+) — 409 s'il contient des questions")
def delete_perimeter(perimeter_id: int, principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    try:
        perimeters_svc.delete(db, principal.org.id, perimeter_id)
    except ValueError as exc:
        raise _value_error(exc)
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action="delete", entity_type="perimeter",
                 entity_id=perimeter_id, meta=principal.audit_meta())
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- Pools abonnés et questions effectives (E4) ---------------------
def _pool_error(exc: pools.PoolError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


@router.get("/{perimeter_id}/pools", response_model=list[SubscriptionOut], summary="Pools abonnés au périmètre")
def list_subscriptions(perimeter_id: int, principal: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    p = _get_or_404(db, principal.org.id, perimeter_id)
    return [
        SubscriptionOut(pool_id=s.pool.id, pool_name=s.pool.name, owner_org_slug=s.owner.slug,
                        visible=s.visible, n_questions=s.n_questions)
        for s in pools.subscriptions(db, p)
    ]


@router.post("/{perimeter_id}/pools", response_model=list[SubscriptionOut], status_code=status.HTTP_201_CREATED,
             summary="Abonner un pool visible au périmètre (editor+) — idempotent")
def subscribe_pool(perimeter_id: int, body: PoolRefIn, principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    p = _get_or_404(db, principal.org.id, perimeter_id)
    try:
        pool = pools.subscribe(db, p, body.pool_id, created_by=principal.user_id)
    except pools.PoolError as exc:
        raise _pool_error(exc)
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action="subscribe_pool", entity_type="perimeter",
                 entity_id=p.id, meta={"pool_id": pool.id, **principal.audit_meta()})
    return list_subscriptions(perimeter_id, principal, db)


@router.delete("/{perimeter_id}/pools/{pool_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Désabonner un pool (editor+)")
def unsubscribe_pool(perimeter_id: int, pool_id: int, principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    p = _get_or_404(db, principal.org.id, perimeter_id)
    pools.unsubscribe(db, p, pool_id)
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action="unsubscribe_pool", entity_type="perimeter",
                 entity_id=p.id, meta={"pool_id": pool_id, **principal.audit_meta()})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{perimeter_id}/effective-questions", response_model=list[EffectiveQuestionOut],
            summary="Questions qu'un run du périmètre exécuterait : propres + pools abonnés (actives et notables)")
def effective_questions(perimeter_id: int, principal: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    p = _get_or_404(db, principal.org.id, perimeter_id)
    origins = pools.origins_for(db, p)
    tests = pools.effective_tests(db, p)
    tmap = themes.for_tests(db, [t.test_id for t in tests])
    out = []
    for t in tests:
        q = EffectiveQuestionOut.model_validate(t)
        q.is_active = t.status == "published"
        q.own = t.perimeter_id == p.id
        q.pools = origins.get(t.test_id, [])
        q.theme_ids = [th.id for th in tmap.get(t.test_id, [])]
        out.append(q)
    return out
