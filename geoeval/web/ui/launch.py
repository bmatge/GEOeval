"""Lancer une évaluation (formulaire + soumission).

Router UI (lot 1.3a). Toutes les règles (liste blanche, validation, devis,
plafond budgétaire) sont dans `geoeval.web.launching` ; ce module ne fait que
lire le formulaire et traduire les refus en HTTP.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from geoeval.core.load import load_tests
from geoeval.web import agreement, budget, launching, perimeters
from geoeval.web.auth import CurrentUser
from geoeval.web.deps import get_db, require_role, require_user
from geoeval.web.rendering import render

router = APIRouter()


def http_error(exc: launching.LaunchError) -> HTTPException:
    """Traduction UI d'un refus de lancement (400 / 402 / 403 / 404)."""
    return HTTPException(status_code=exc.status, detail=exc.detail)


def run_form_context(
    db: Session,
    org_id: int,
    perimeter_id: Optional[int] = None,
    *,
    role: Optional[str] = None,
    is_platform_admin: bool = False,
) -> dict:
    """Contexte commun aux formulaires « lancer » et « planifier » un run.

    Si `perimeter_id` est fourni, seules les questions de ce périmètre sont exposées.
    La liste blanche org_models (EPIC-001 S4.2) restreint les modèles proposés
    (testés ET notateurs) aux rôles editor/viewer.
    """
    models = launching.allowed_models(db, org_id, role=role, is_platform_admin=is_platform_admin)
    judges = [m for m in models if m.is_judge]
    judge_kappa: dict[int, Optional[float]] = {}
    for j in judges:
        ag = agreement.compute_agreement_vs_gold(db, j.model_id)
        judge_kappa[j.model_id] = ag.get("response_kappa")
    all_peri = perimeters.list_for_org(db, org_id)
    all_tests = load_tests(db, organization_id=org_id)
    tests_for_form = all_tests if perimeter_id is None else [t for t in all_tests if t.perimeter_id == perimeter_id]
    return dict(
        models=models,
        testable_models=[m for m in models if (m.model_name or "").lower() in launching.TESTABLE_PROVIDERS],
        judgeable_models=judges,
        judge_kappa=judge_kappa,
        kappa_threshold=0.6,
        tests=tests_for_form,
        all_perimeters=all_peri,
    )


@router.get("/o/{org_slug}/launch", response_class=HTMLResponse)
def launch_form(
    request: Request,
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
    perimeter_id: int = 0,
):
    org, role = ctx
    peri = perimeters.get_by_id(db, org.id, perimeter_id) if perimeter_id > 0 else None
    fctx = run_form_context(
        db, org.id, perimeter_id=peri.id if peri else None,
        role=role, is_platform_admin=user.is_platform_admin,
    )
    b = budget.get_budget(db, org.id)
    spent = budget.current_period_spent(db, org.id, "month")
    day_spent = budget.current_period_spent(db, org.id, "day")
    return render(request, "launch.html", active="launch", org=org, role=role,
                  active_tests_count=len(fctx["tests"]),
                  selected_perimeter=peri,
                  budget=b, month_spent=spent, day_spent=day_spent, **fctx)


@router.post("/o/{org_slug}/launch")
def launch_submit(
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
    perimeter_id: int = Form(...),
    tested_models: list[str] = Form(default=[]),
    judge_models: list[str] = Form(default=[]),
    repeats: int = Form(1),
    note: str = Form(""),
    test_ids: list[int] = Form(default=[]),
):
    org, role = ctx
    try:
        job = launching.launch_run(
            db, org.id, perimeter_id=perimeter_id, tested_models=tested_models,
            judge_models=judge_models, repeats=repeats, test_ids=test_ids, note=note or None,
            role=role, is_platform_admin=user.is_platform_admin,
        )
    except launching.LaunchError as exc:
        raise http_error(exc)
    return RedirectResponse(f"/o/{org.slug}/jobs/{job.id}", status_code=303)
