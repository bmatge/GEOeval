"""Accueil d'organisation, tableau de bord et API JSON interne des stats (dsfr-data).

Router UI (lot 1.3a) : découpage mécanique de l'ancien app.py, sans changement
de comportement. Les règles métier vivent dans les services (geoeval.web.*).
"""
from __future__ import annotations


from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from geoeval.core.load import load_tests
from geoeval.web import (
    budget,
    services,
)
from geoeval.web.deps import (
    get_db,
    public_org,
)
from geoeval.web.rendering import render

router = APIRouter()


@router.get("/o/{org_slug}/", response_class=HTMLResponse)
def org_home(request: Request, ctx=Depends(public_org), db: Session = Depends(get_db)):
    org, role = ctx
    return render(
        request,
        "home.html",
        active="home",
        org=org,
        role=role,
        n_models=len(services.list_models(db)),
        n_tests=len(load_tests(db, organization_id=org.id)),
        n_runs=len(services.list_runs(db, org.id)),
    )


@router.get("/o/{org_slug}/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, ctx=Depends(public_org), db: Session = Depends(get_db)):
    org, role = ctx
    return render(
        request,
        "dashboard.html",
        active="dashboard",
        org=org,
        role=role,
        leaderboard=services.leaderboard(db, org.id),
        runs=services.list_runs(db, org.id)[:5],
        # E3 : bandeaux budgétaires pour les membres (jamais pour un visiteur anonyme).
        budget_alerts=budget.chain_alerts(db, org) if role is not None else [],
    )

@router.get("/o/{org_slug}/api/stats/summary")
def api_stats_summary(request: Request, ctx=Depends(public_org), db: Session = Depends(get_db)):
    org, _ = ctx
    # Tableau à un élément : format attendu par dsfr-data-kpi (1er enregistrement).
    return JSONResponse([services.org_stats_summary(db, org.id)])


@router.get("/o/{org_slug}/api/stats/leaderboard")
def api_stats_leaderboard(request: Request, ctx=Depends(public_org), db: Session = Depends(get_db)):
    org, _ = ctx
    return JSONResponse(services.leaderboard(db, org.id))


@router.get("/o/{org_slug}/api/stats/questions")
def api_stats_questions(request: Request, ctx=Depends(public_org), db: Session = Depends(get_db)):
    """Scores moyens par question, pires d'abord (barres horizontales)."""
    org, _ = ctx
    return JSONResponse(services.question_stats(db, org.id))


@router.get("/o/{org_slug}/explore", response_class=HTMLResponse)
def explore(request: Request, ctx=Depends(public_org), db: Session = Depends(get_db), history: int = 0):
    """Explorateur de scores : recherche, facettes (entité, périmètre, IA), tableau et matrice."""
    org, role = ctx
    return render(request, "explore.html", active="explore", org=org, role=role, history=bool(history),
                  limit=services.SCORE_ROWS_LIMIT)


@router.get("/o/{org_slug}/api/stats/scores")
def api_stats_scores(request: Request, ctx=Depends(public_org), db: Session = Depends(get_db), history: int = 0):
    """Une ligne par (évaluation, question) de l'entité et de son sous-arbre (explorateur de scores)."""
    org, _ = ctx
    return JSONResponse(services.score_rows(db, org, history=bool(history)))


@router.get("/o/{org_slug}/api/stats/evolution")
def api_stats_evolution(request: Request, ctx=Depends(public_org), db: Session = Depends(get_db)):
    """Scores par IA et par n° de passage (format long — courbes par IA)."""
    org, _ = ctx
    return JSONResponse(services.model_evolution(db, org.id))


@router.get("/o/{org_slug}/api/stats/runs")
def api_stats_runs(request: Request, ctx=Depends(public_org), db: Session = Depends(get_db)):
    """Évaluations en ordre chronologique (axe X des courbes d'évolution)."""
    org, _ = ctx
    rows = services.list_runs(db, org.id)
    out = [
        dict(
            run_id=r["run_id"],
            label=(
                f"#{r['run_id']} · {r['started_at'].strftime('%d/%m')}"
                if r["started_at"] else f"#{r['run_id']}"
            ),
            model=r["model_version"],
            n_evals=r["n_evals"],
            avg_response=r["avg_response"],
            avg_citation=r["avg_citation"],
        )
        for r in reversed(rows)
    ]
    return JSONResponse(out)
