"""Page méthodologie : accord des juges avec le gold set (ADR-079 §5).

Router UI (lot 1.3a) : découpage mécanique de l'ancien app.py, sans changement
de comportement. Les règles métier vivent dans les services (geoeval.web.*).
"""
from __future__ import annotations


from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from geoeval.db.models import Model
from geoeval.web import (
    agreement,
)
from geoeval.web.auth import CurrentUser
from geoeval.web.deps import (
    get_db,
    require_user,
)
from geoeval.web.rendering import nav_fallback, render

router = APIRouter()


KAPPA_WARNING_THRESHOLD = 0.6

def _judges_agreement_matrix(session):
    """Renvoie [{model, agreement}] pour tous les modèles marqués juge."""
    judges = session.execute(
        select(Model).where(Model.is_judge.is_(True))
    ).scalars().all()
    out = []
    for j in judges:
        ag = agreement.compute_agreement_vs_gold(session, j.model_id)
        out.append(dict(
            model_id=j.model_id,
            model_name=j.model_name,
            model_version=j.model_version,
            is_sovereign=j.is_sovereign,
            agreement=ag,
        ))
    return out


@router.get("/methodology/judges", response_class=HTMLResponse)
def methodology_judges(
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    # Accessible à tout user connecté (info méthodologique).
    matrix = _judges_agreement_matrix(db)
    nav_org, nav_role = nav_fallback(db, user)
    return render(
        request, "methodology_judges.html", active="methodology",
        org=nav_org, role=nav_role,
        matrix=matrix, threshold=KAPPA_WARNING_THRESHOLD,
    )
