"""Évaluations (runs) : liste et détail, lecture publique (ADR-087).

Router UI (lot 1.3a) : découpage mécanique de l'ancien app.py, sans changement
de comportement. Les règles métier vivent dans les services (geoeval.web.*).
"""
from __future__ import annotations


from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from geoeval.web import (
    services,
)
from geoeval.web.deps import (
    get_db,
    public_org,
)
from geoeval.web.rendering import render

router = APIRouter()


@router.get("/o/{org_slug}/runs", response_class=HTMLResponse)
def runs(request: Request, ctx=Depends(public_org), db: Session = Depends(get_db)):
    org, role = ctx
    return render(
        request, "runs.html", active="runs", org=org, role=role,
        runs=services.list_runs(db, org.id),
    )


@router.get("/o/{org_slug}/runs/{run_id}", response_class=HTMLResponse)
def run_detail(run_id: int, request: Request, ctx=Depends(public_org), db: Session = Depends(get_db)):
    org, role = ctx
    detail = services.get_run_detail(db, org.id, run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} introuvable")
    from geoeval.web import calibration, rejudge

    return render(request, "run_detail.html", active="runs", org=org, role=role, run=detail,
                  annotated=calibration.annotated_test_ids(db, run_id) if role else {},
                  batches=rejudge.runs_with_batches(db, [run_id]).get(run_id, []) if role else [])
