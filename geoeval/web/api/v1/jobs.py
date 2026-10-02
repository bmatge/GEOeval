"""Jobs d'évaluation (file persistée, lot 1.2) — lecture, membres."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from geoeval.db.models import Job
from geoeval.web.api.deps import Principal, require_role
from geoeval.web.api.schemas import JobOut, Page
from geoeval.web.deps import get_db
from geoeval.worker import jobs as jobqueue

router = APIRouter(prefix="/orgs/{org_slug}/jobs", tags=["jobs"])


@router.get("", response_model=Page[JobOut], summary="Jobs de l'organisation, plus récents d'abord (sans logs)")
def list_jobs(
    principal: Principal = Depends(require_role("viewer")),
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    total = db.execute(select(func.count()).select_from(Job).where(Job.organization_id == principal.org.id)).scalar_one()
    rows = db.execute(
        select(Job).where(Job.organization_id == principal.org.id).order_by(Job.created_at.desc()).offset(offset).limit(limit)
    ).scalars().all()
    return dict(items=[jobqueue.as_dict(j) for j in rows], total=int(total), limit=limit, offset=offset)


@router.get("/{job_id}", response_model=JobOut, summary="Un job avec la fin de ses logs")
def get_job(job_id: str, principal: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    job = jobqueue.get_for_org(db, principal.org.id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job introuvable.")
    return jobqueue.as_dict(job, log=jobqueue.tail_logs(db, job.id))
