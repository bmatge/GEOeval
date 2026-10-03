"""Catalogue global des thèmes (E4) : lecture pour tout principal authentifié,
écriture réservée à l'administration plateforme (arbitrage E4)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from geoeval.web import audit, themes
from geoeval.web.api.deps import Actor, require_platform_admin, structure_actor
from geoeval.web.api.schemas import ThemeIn, ThemeOut, ThemePatch
from geoeval.web.auth import CurrentUser
from geoeval.web.deps import get_db

router = APIRouter(prefix="/themes", tags=["thèmes"])


def _http(exc: themes.ThemeError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


def _get_or_404(db: Session, theme_id: int):
    th = themes.get(db, theme_id)
    if th is None:
        raise HTTPException(status_code=404, detail="Thème introuvable.")
    return th


@router.get("", response_model=list[ThemeOut], summary="Catalogue des thèmes (tout principal authentifié)")
def list_themes(_actor: Actor = Depends(structure_actor), db: Session = Depends(get_db)):
    return [ThemeOut.model_validate(t) for t in themes.list_all(db)]


@router.post("", response_model=ThemeOut, status_code=status.HTTP_201_CREATED,
             summary="Créer un thème (administration plateforme)")
def create_theme(body: ThemeIn, user: CurrentUser = Depends(require_platform_admin), db: Session = Depends(get_db)):
    try:
        th = themes.create(db, label=body.label, slug=body.slug)
    except themes.ThemeError as exc:
        raise _http(exc)
    audit.record(db, user_id=user.id, org_id=None, action="create", entity_type="theme", entity_id=th.id,
                 meta={"slug": th.slug, "via": "api"})
    return ThemeOut.model_validate(th)


@router.patch("/{theme_id}", response_model=ThemeOut, summary="Renommer un thème (administration plateforme)")
def rename_theme(theme_id: int, body: ThemePatch, user: CurrentUser = Depends(require_platform_admin),
                 db: Session = Depends(get_db)):
    th = _get_or_404(db, theme_id)
    try:
        themes.rename(db, th, body.label)
    except themes.ThemeError as exc:
        raise _http(exc)
    audit.record(db, user_id=user.id, org_id=None, action="update", entity_type="theme", entity_id=th.id,
                 meta={"label": th.label, "via": "api"})
    return ThemeOut.model_validate(th)


@router.delete("/{theme_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Supprimer un thème et ses étiquettes (administration plateforme)")
def delete_theme(theme_id: int, user: CurrentUser = Depends(require_platform_admin), db: Session = Depends(get_db)):
    th = _get_or_404(db, theme_id)
    counts = themes.delete(db, th)
    audit.record(db, user_id=user.id, org_id=None, action="delete", entity_type="theme", entity_id=theme_id,
                 meta={**counts, "via": "api"})
    return Response(status_code=status.HTTP_204_NO_CONTENT)
