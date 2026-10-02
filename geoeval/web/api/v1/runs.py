"""Évaluations (runs) : lecture publique (ADR-087) et lancement (editor+)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from geoeval.web import audit, launching, services
from geoeval.web.api.deps import Principal, org_context, require_role
from geoeval.web.api.schemas import JobOut, LaunchIn, Page, RunDetailOut, RunOut, paginate
from geoeval.web.deps import get_db
from geoeval.worker import jobs as jobqueue

router = APIRouter(prefix="/orgs/{org_slug}/runs", tags=["évaluations"])


@router.get("", response_model=Page[RunOut], summary="Évaluations, plus récentes d'abord (lecture publique)")
def list_runs(
    principal: Principal = Depends(org_context),
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    return paginate(services.list_runs(db, principal.org.id), limit=limit, offset=offset)


@router.get("/{run_id}", response_model=RunDetailOut, summary="Détail d'une évaluation : réponses, citations, notes")
def get_run(run_id: int, principal: Principal = Depends(org_context), db: Session = Depends(get_db)):
    detail = services.get_run_detail(db, principal.org.id, run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Évaluation {run_id} introuvable.")
    return detail


@router.post(
    "", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED,
    summary="Lancer une évaluation (editor+) — mise en file, suivi via /jobs/{id}",
    responses={402: {"description": "Plafond budgétaire atteint"}, 403: {"description": "Modèles hors liste blanche ou rôle insuffisant"}},
)
def launch(body: LaunchIn, principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    """Toutes les règles (liste blanche, périmètre, devis, budget) sont celles du
    service `launching`, identiques à l'UI. Les refus arrivent en problem+json."""
    job = launching.launch_run(
        db, principal.org.id, perimeter_id=body.perimeter_id, tested_models=body.tested_models,
        judge_models=body.judge_models, repeats=body.repeats, test_ids=body.test_ids, note=body.note,
        role=principal.role, is_platform_admin=principal.is_platform_admin,
    )
    audit.record(
        db, user_id=principal.user_id, org_id=principal.org.id, action="launch", entity_type="job",
        meta={"job_id": job.id, "tested_models": body.tested_models, **principal.audit_meta()},
    )
    return jobqueue.as_dict(job)
