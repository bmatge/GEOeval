"""Rejugement versionné (suite E8, ADR-089 §2.9) et calibration des notateurs.

Router UI mince : les règles vivent dans `geoeval.web.rejudge` et
`geoeval.web.calibration`. Lecture : membres de l'entité. Lancer un rejugement,
annoter : editor+.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from geoeval.core.evaluate import CONFORMITY_LABELS
from geoeval.db.models import EvaluationPrompt, Model, RunResult, RunRow, Test
from geoeval.web import audit, calibration, detectors, launching, rejudge, services
from geoeval.web.auth import CurrentUser
from geoeval.web.deps import get_db, require_role, require_user
from geoeval.web.rendering import render
from geoeval.web.ui.launch import http_error

router = APIRouter()

RECENT_RUNS = 100


def _form_ctx(db: Session, org, role, user: CurrentUser) -> dict:
    models = launching.allowed_models(db, org.id, role=role, is_platform_admin=user.is_platform_admin)
    prompts = db.execute(select(EvaluationPrompt).order_by(EvaluationPrompt.prompt_id)).scalars().all()
    return dict(
        runs=services.list_runs(db, org.id)[:RECENT_RUNS],
        judge_models=[m for m in models if m.is_judge],
        response_prompts=[p for p in prompts if p.prompt_type_id == rejudge.RESPONSE_PROMPT_TYPE],
        citation_prompts=[p for p in prompts if p.prompt_type_id == rejudge.CITATION_PROMPT_TYPE],
        max_runs=rejudge.MAX_RUNS, max_repeats=rejudge.MAX_REPEATS,
    )


# ---------------------------------------------------------------------
# Rejugement
# ---------------------------------------------------------------------
@router.get("/o/{org_slug}/rejudge", response_class=HTMLResponse)
def batches_list(request: Request, ctx=Depends(require_role("viewer")), db: Session = Depends(get_db)):
    org, role = ctx
    items = rejudge.list_for_org(db, org)
    return render(request, "rejudge_list.html", active="rejudge", org=org, role=role, items=items,
                  statuses={b.id: rejudge.status(db, b) for b in items}, judges_label=rejudge.judges_label)


@router.get("/o/{org_slug}/rejudge/new", response_class=HTMLResponse)
def batch_form(request: Request, ctx=Depends(require_role("editor")), db: Session = Depends(get_db),
               user: CurrentUser = Depends(require_user), run_id: Optional[int] = None):
    org, role = ctx
    return render(request, "rejudge_form.html", active="rejudge", org=org, role=role,
                  selected_runs={run_id} if run_id else set(), **_form_ctx(db, org, role, user))


@router.post("/o/{org_slug}/rejudge/new")
def batch_create(
    ctx=Depends(require_role("editor")), db: Session = Depends(get_db), user: CurrentUser = Depends(require_user),
    run_ids: list[int] = Form(default=[]), judge_models: list[str] = Form(default=[]), repeats: int = Form(1),
    response_prompt_id: int = Form(0), citation_prompt_id: int = Form(0), label: str = Form(""),
):
    org, role = ctx
    try:
        b = rejudge.create(
            db, org, run_ids=run_ids, judge_models=judge_models, repeats=repeats,
            response_prompt_id=response_prompt_id or None, citation_prompt_id=citation_prompt_id or None,
            label=label, created_by=user.id, role=role, is_platform_admin=user.is_platform_admin,
        )
    except launching.LaunchError as exc:
        raise http_error(exc)
    audit.record(db, user_id=user.id, org_id=org.id, action="create", entity_type="evaluation_batch", entity_id=b.id,
                 meta={"runs": b.run_ids, "judges": [j["model_version"] for j in b.judges],
                       "estimate_eur": str(b.estimate_eur), "job_id": b.job_id})
    return RedirectResponse(f"/o/{org.slug}/rejudge/{b.id}", status_code=303)


@router.get("/o/{org_slug}/rejudge/{batch_id}", response_class=HTMLResponse)
def batch_detail(batch_id: int, request: Request, ctx=Depends(require_role("viewer")), db: Session = Depends(get_db)):
    org, role = ctx
    try:
        b = rejudge.get_for_org(db, org, batch_id)
    except launching.LaunchError as exc:
        raise http_error(exc)
    prompts = {p.prompt_id: p for p in db.execute(select(EvaluationPrompt).where(
        EvaluationPrompt.prompt_id.in_([i for i in (b.response_prompt_id, b.citation_prompt_id) if i] or [-1]))).scalars()}
    return render(request, "rejudge_detail.html", active="rejudge", org=org, role=role, batch=b,
                  status=rejudge.status(db, b), comparison=rejudge.compare(db, b), prompts=prompts,
                  judges_label=rejudge.judges_label(b), estimate=rejudge.estimate_display(b), fmt=calibration.fmt)


# ---------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------
@router.get("/o/{org_slug}/calibration", response_class=HTMLResponse)
def calibration_page(request: Request, ctx=Depends(require_role("viewer")), db: Session = Depends(get_db)):
    org, role = ctx
    eff = detectors.effective_settings(db, org)
    _, min_pairs = detectors.calibration_defaults()
    return render(request, "calibration.html", active="calibration", org=org, role=role,
                  judges=calibration.overview(db, org), n_gold=len(calibration.gold_pairs(db, org)),
                  own=calibration.list_annotations(db, org, own_only=True, limit=50),
                  threshold=eff.calibration_min_rho, threshold_from=eff.calibration_from, min_pairs=min_pairs,
                  fmt=calibration.fmt)


def _annotate_ctx(db: Session, org, run_id: int, test_id: int):
    run = db.get(RunRow, run_id)
    result = db.get(RunResult, (run_id, test_id))
    if run is None or run.organization_id != org.id or result is None:
        raise HTTPException(status_code=404, detail="Résultat introuvable.")
    return run, result, db.get(Test, test_id), db.get(Model, run.tested_model_id)


@router.get("/o/{org_slug}/runs/{run_id}/tests/{test_id}/annotate", response_class=HTMLResponse)
def annotate_form(run_id: int, test_id: int, request: Request, ctx=Depends(require_role("editor")),
                  db: Session = Depends(get_db), user: CurrentUser = Depends(require_user)):
    org, role = ctx
    run, result, test, model = _annotate_ctx(db, org, run_id, test_id)
    return render(request, "annotate.html", active="calibration", org=org, role=role, run=run, result=result,
                  test=test, model=model, labels=CONFORMITY_LABELS,
                  mine=calibration.annotation_of(db, run_id, test_id, user.email))


@router.post("/o/{org_slug}/runs/{run_id}/tests/{test_id}/annotate")
def annotate_submit(
    run_id: int, test_id: int, ctx=Depends(require_role("editor")), db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user), response_label: str = Form(...), response_score: str = Form(...),
    citation_label: str = Form(...), citation_score: str = Form(...), notes: str = Form(""),
):
    org, _ = ctx
    try:
        a = calibration.annotate(db, org, run_id=run_id, test_id=test_id, user_id=user.id, email=user.email,
                                 response_label=response_label, response_score=response_score,
                                 citation_label=citation_label, citation_score=citation_score, notes=notes)
    except calibration.CalibrationError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    audit.record(db, user_id=user.id, org_id=org.id, action="annotate", entity_type="gold_annotation",
                 entity_id=a.id, meta={"run_id": run_id, "test_id": test_id})
    return RedirectResponse(f"/o/{org.slug}/runs/{run_id}", status_code=303)
