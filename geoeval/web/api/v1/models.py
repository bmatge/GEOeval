"""Catalogue des modèles, filtré par la liste blanche de l'organisation selon le rôle."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from geoeval.web import launching
from geoeval.web.api.deps import Principal, require_role
from geoeval.web.api.schemas import ModelOut
from geoeval.web.deps import get_db

router = APIRouter(prefix="/orgs/{org_slug}/models", tags=["modèles"])


@router.get("", response_model=list[ModelOut], summary="Modèles utilisables par l'organisation")
def list_models(principal: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Même règle que l'UI : editor et viewer voient la liste blanche de l'org,
    org_admin et admin plateforme tout le catalogue actif. Aucune clé n'est exposée."""
    models = launching.allowed_models(db, principal.org.id, role=principal.role, is_platform_admin=principal.is_platform_admin)
    out = []
    for m in models:
        o = ModelOut.model_validate(m)
        o.testable = (m.model_name or "").lower() in launching.TESTABLE_PROVIDERS
        out.append(o)
    return out
