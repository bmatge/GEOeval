"""Suivi des jobs d'évaluation (file persistée, lot 1.2).

Router UI (lot 1.3a) : découpage mécanique de l'ancien app.py, sans changement
de comportement. Les règles métier vivent dans les services (geoeval.web.*).
"""
from __future__ import annotations


from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from geoeval.web.deps import (
    get_db,
    require_org,
)
from geoeval.web.rendering import render
from geoeval.worker import jobs as jobqueue

router = APIRouter()


@router.get("/o/{org_slug}/jobs", response_class=HTMLResponse)
def jobs_list(request: Request, ctx=Depends(require_org), db: Session = Depends(get_db)):
    org, role = ctx
    return render(request, "jobs.html", active="launch", org=org, role=role,
                  jobs=[jobqueue.as_dict(j) for j in jobqueue.list_for_org(db, org.id)])


@router.get("/o/{org_slug}/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(job_id: str, request: Request, ctx=Depends(require_org), db: Session = Depends(get_db)):
    org, role = ctx
    job = jobqueue.get_for_org(db, org.id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} introuvable")
    return render(request, "job_detail.html", active="launch", org=org, role=role,
                  job=jobqueue.as_dict(job, log=jobqueue.tail_logs(db, job.id)))


@router.get("/o/{org_slug}/api/jobs/{job_id}")
def job_status(job_id: str, ctx=Depends(require_org), db: Session = Depends(get_db)):
    org, _ = ctx
    job = jobqueue.get_for_org(db, org.id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job introuvable")
    return JSONResponse(jobqueue.as_dict(job, log=jobqueue.tail_logs(db, job.id)))
