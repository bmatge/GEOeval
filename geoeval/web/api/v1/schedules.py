"""Planifications — lecture (membres) et exécution immédiate (editor+)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from geoeval.web import audit, launching, services
from geoeval.web.api.deps import Principal, require_role
from geoeval.web.api.schemas import JobOut, ScheduleOut
from geoeval.web.deps import get_db
from geoeval.worker import jobs as jobqueue
from geoeval.worker import scheduler

router = APIRouter(prefix="/orgs/{org_slug}/schedules", tags=["planifications"])


def _out(sr) -> ScheduleOut:
    out = ScheduleOut.model_validate(sr)
    out.description = scheduler.describe_schedule(sr.schedule_kind, sr.schedule_config)
    return out


@router.get("", response_model=list[ScheduleOut], summary="Planifications de l'organisation")
def list_schedules(principal: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    return [_out(sr) for sr in services.list_schedules(db, principal.org.id)]


@router.get("/{schedule_id}", response_model=ScheduleOut, summary="Une planification")
def get_schedule(schedule_id: int, principal: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    sr = services.get_schedule(db, principal.org.id, schedule_id)
    if sr is None:
        raise HTTPException(status_code=404, detail="Planification introuvable.")
    return _out(sr)


@router.post("/{schedule_id}/run-now", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED,
             summary="Exécuter maintenant (editor+), soumis au plafond budgétaire")
def run_now(schedule_id: int, principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    sr = services.get_schedule(db, principal.org.id, schedule_id)
    if sr is None:
        raise HTTPException(status_code=404, detail="Planification introuvable.")
    job = launching.run_schedule_now(db, principal.org.id, sr)
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action="run_now", entity_type="scheduled_run",
                 entity_id=sr.schedule_id, meta={"job_id": job.id, **principal.audit_meta()})
    return jobqueue.as_dict(job)
