"""
Rendu Jinja2 + helpers partagés par les routers UI (geoeval.web.ui.*).

Séparé de app.py pour que chaque router importe ce module sans cycle
(lot 1.3a, ADR-088 §2.3 : UI mince, règles dans les services).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from geoeval.db.models import Organization
from geoeval.web.auth import CurrentUser

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def opt_int(value: Optional[str]) -> Optional[int]:
    """Champ de formulaire optionnel → int ou None."""
    if value is None or value.strip() == "":
        return None
    return int(value)


def render(
    request: Request,
    template: str,
    active: str,
    org=None,
    role: Optional[str] = None,
    **ctx,
) -> HTMLResponse:
    """Rendu Jinja + injection tenancy commune.

    org/role passés ici sont propagés au template via `url_prefix` et un dict
    `org` (id, name, slug) pour la nav DSFR.
    """
    user: Optional[CurrentUser] = getattr(request.state, "user", None)
    context = {
        "active": active,
        "user": user,
        "org": {"id": org.id, "name": org.name, "slug": org.slug} if org is not None else None,
        "role": role,
        "url_prefix": f"/o/{org.slug}" if org is not None else "",
        **ctx,
    }
    return templates.TemplateResponse(request=request, name=template, context=context)


def nav_fallback(db: Session, user: Optional[CurrentUser]) -> tuple:
    """Org de repli pour la nav des pages hors organisation (/admin/*, méthodologie).

    Première adhésion de l'utilisateur ; pour un admin plateforme sans adhésion,
    la première organisation existante. (None, None) sinon — la nav reste masquée.
    """
    if user is None:
        return (None, None)
    if user.memberships:
        org_id = next(iter(user.memberships))
        return (db.get(Organization, org_id), user.memberships.get(org_id))
    if user.is_platform_admin:
        org = db.execute(select(Organization).order_by(Organization.id)).scalars().first()
        return (org, None)
    return (None, None)
