"""Accueil (choix d'organisation) et redirections 301 des anciennes URL.

Router UI (lot 1.3a) : découpage mécanique de l'ancien app.py, sans changement
de comportement. Les règles métier vivent dans les services (geoeval.web.*).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from geoeval.web import (
    tenancy,
)
from geoeval.web.auth import CurrentUser
from geoeval.web.deps import (
    get_db,
)
from geoeval.web.rendering import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    user: Optional[CurrentUser] = getattr(request.state, "user", None)
    # Visiteur anonyme : landing publique listant les organisations (les
    # tableaux de bord et évaluations sont consultables sans compte — ADR-087).
    if user is None or user.is_platform_admin:
        orgs = tenancy.list_all_orgs(db)
    else:
        orgs = tenancy.list_orgs_for_user(db, user.id)

    # Un seul choix : redirige direct sur son dashboard.
    if user is not None and len(orgs) == 1:
        return RedirectResponse(f"/o/{orgs[0].slug}/", status_code=302)

    return templates.TemplateResponse(
        request=request,
        name="landing.html",
        context={
            "active": "home",
            "user": user,
            "orgs": orgs,
            "url_prefix": "",
        },
    )

_LEGACY_PATHS = (
    "dashboard",
    "runs",
    "tests",
    "prompts",
    "models",
    "launch",
    "schedules",
    "jobs",
)


def _primary_org_slug(request: Request, db: Session) -> Optional[str]:
    user: Optional[CurrentUser] = getattr(request.state, "user", None)
    if user is None:
        return None
    if user.memberships_by_slug:
        return next(iter(user.memberships_by_slug))
    if user.is_platform_admin:
        orgs = tenancy.list_all_orgs(db)
        if orgs:
            return orgs[0].slug
    return None


def _install_legacy_redirect(path: str) -> None:
    @router.get(f"/{path}")
    def _redir(request: Request, db: Session = Depends(get_db)):  # noqa: ANN001
        slug = _primary_org_slug(request, db)
        if slug is None:
            return RedirectResponse("/", status_code=302)
        return RedirectResponse(f"/o/{slug}/{path}", status_code=301)


for _p in _LEGACY_PATHS:
    _install_legacy_redirect(_p)
