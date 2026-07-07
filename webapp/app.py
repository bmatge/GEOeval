"""
UI web GEOeval — FastAPI + Jinja2 + DSFR.

Lancement :
    uvicorn webapp.app:app --reload
ou :
    python run_web.py
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from db import SessionLocal
from load import load_tests
from webapp import services
from webapp.jobs import manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logging.getLogger().setLevel(logging.INFO)  # au cas où l'entrypoint a déjà configuré le root

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="GEOeval")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _opt_int(value: Optional[str]) -> Optional[int]:
    if value is None or value.strip() == "":
        return None
    return int(value)


def render(request: Request, template: str, active: str, **ctx) -> HTMLResponse:
    context = {"active": active, **ctx}
    return templates.TemplateResponse(request=request, name=template, context=context)


# -----------------------------
# Tableau de bord
# -----------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    return render(
        request,
        "dashboard.html",
        active="dashboard",
        leaderboard=services.leaderboard(db),
        runs=services.list_runs(db)[:5],
    )


# -----------------------------
# Runs
# -----------------------------
@app.get("/runs", response_class=HTMLResponse)
def runs(request: Request, db: Session = Depends(get_db)):
    return render(request, "runs.html", active="runs", runs=services.list_runs(db))


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(run_id: int, request: Request, db: Session = Depends(get_db)):
    detail = services.get_run_detail(db, run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} introuvable")
    return render(request, "run_detail.html", active="runs", run=detail)


# -----------------------------
# Tests
# -----------------------------
@app.get("/tests", response_class=HTMLResponse)
def tests(request: Request, db: Session = Depends(get_db)):
    return render(request, "tests.html", active="tests", tests=services.list_tests(db))


@app.get("/tests/new", response_class=HTMLResponse)
def test_new(request: Request, db: Session = Depends(get_db)):
    return render(
        request,
        "test_form.html",
        active="tests",
        test=None,
        prompts=services.list_prompts(db),
    )


@app.post("/tests/new")
def test_create(
    db: Session = Depends(get_db),
    prompt: str = Form(...),
    expected_answer: str = Form(""),
    response_quality_prompt_id: str = Form(""),
    citation_quality_prompt_id: str = Form(""),
):
    services.create_test(
        db,
        prompt=prompt,
        expected_answer=expected_answer,
        response_quality_prompt_id=_opt_int(response_quality_prompt_id),
        citation_quality_prompt_id=_opt_int(citation_quality_prompt_id),
    )
    return RedirectResponse("/tests", status_code=303)


@app.get("/tests/{test_id}/edit", response_class=HTMLResponse)
def test_edit(test_id: int, request: Request, db: Session = Depends(get_db)):
    test = services.get_test(db, test_id)
    if test is None:
        raise HTTPException(status_code=404, detail=f"Test {test_id} introuvable")
    return render(
        request,
        "test_form.html",
        active="tests",
        test=test,
        prompts=services.list_prompts(db),
    )


@app.post("/tests/{test_id}/edit")
def test_update(
    test_id: int,
    db: Session = Depends(get_db),
    prompt: str = Form(...),
    expected_answer: str = Form(""),
    response_quality_prompt_id: str = Form(""),
    citation_quality_prompt_id: str = Form(""),
):
    services.update_test(
        db,
        test_id,
        prompt=prompt,
        expected_answer=expected_answer,
        response_quality_prompt_id=_opt_int(response_quality_prompt_id),
        citation_quality_prompt_id=_opt_int(citation_quality_prompt_id),
    )
    return RedirectResponse("/tests", status_code=303)


@app.post("/tests/{test_id}/deactivate")
def test_deactivate(test_id: int, db: Session = Depends(get_db)):
    services.deactivate_test(db, test_id)
    return RedirectResponse("/tests", status_code=303)


@app.post("/tests/{test_id}/reactivate")
def test_reactivate(test_id: int, db: Session = Depends(get_db)):
    services.reactivate_test(db, test_id)
    return RedirectResponse("/tests", status_code=303)


# -----------------------------
# Prompts d'évaluation
# -----------------------------
@app.get("/prompts", response_class=HTMLResponse)
def prompts(request: Request, db: Session = Depends(get_db)):
    return render(request, "prompts.html", active="prompts", prompts=services.list_prompts(db))


@app.get("/prompts/new", response_class=HTMLResponse)
def prompt_new(request: Request, db: Session = Depends(get_db)):
    return render(
        request,
        "prompt_form.html",
        active="prompts",
        prompt=None,
        prompt_types=services.list_prompt_types(db),
    )


@app.post("/prompts/new")
def prompt_create(
    db: Session = Depends(get_db),
    prompt_type_id: int = Form(...),
    prompt_name: str = Form(...),
    prompt_text: str = Form(...),
):
    services.create_prompt(
        db, prompt_type_id=prompt_type_id, prompt_name=prompt_name, prompt_text=prompt_text
    )
    return RedirectResponse("/prompts", status_code=303)


@app.get("/prompts/{prompt_id}/edit", response_class=HTMLResponse)
def prompt_edit(prompt_id: int, request: Request, db: Session = Depends(get_db)):
    prompt = services.get_prompt(db, prompt_id)
    if prompt is None:
        raise HTTPException(status_code=404, detail=f"Prompt {prompt_id} introuvable")
    return render(
        request,
        "prompt_form.html",
        active="prompts",
        prompt=prompt,
        prompt_types=services.list_prompt_types(db),
    )


@app.post("/prompts/{prompt_id}/edit")
def prompt_update(
    prompt_id: int,
    db: Session = Depends(get_db),
    prompt_type_id: int = Form(...),
    prompt_name: str = Form(...),
    prompt_text: str = Form(...),
):
    services.update_prompt(
        db,
        prompt_id,
        prompt_type_id=prompt_type_id,
        prompt_name=prompt_name,
        prompt_text=prompt_text,
    )
    return RedirectResponse("/prompts", status_code=303)


# -----------------------------
# Lancer un run
# -----------------------------
@app.get("/launch", response_class=HTMLResponse)
def launch_form(request: Request, db: Session = Depends(get_db)):
    return render(
        request,
        "launch.html",
        active="launch",
        models=services.list_models(db),
        active_tests_count=len(load_tests(db)),
    )


@app.post("/launch")
def launch_submit(
    db: Session = Depends(get_db),
    tested_models: list[str] = Form(default=[]),
    judge_models: list[str] = Form(default=[]),
    repeats: int = Form(1),
    note: str = Form(""),
):
    if not tested_models or not judge_models:
        raise HTTPException(
            status_code=400,
            detail="Sélectionne au moins un modèle testé et un juge.",
        )
    if not load_tests(db):
        raise HTTPException(
            status_code=400,
            detail="Aucun test actif et prêt : active ou crée des tests avant de lancer un run.",
        )
    judges = [{"model": v, "repeats": max(1, repeats)} for v in judge_models]
    job = manager.submit(
        dict(tested_models=tested_models, judges=judges, note=note or None)
    )
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


# -----------------------------
# Jobs (suivi)
# -----------------------------
@app.get("/jobs", response_class=HTMLResponse)
def jobs_list(request: Request):
    return render(request, "jobs.html", active="launch", jobs=[j.as_dict() for j in manager.list()])


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(job_id: str, request: Request):
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} introuvable")
    return render(request, "job_detail.html", active="launch", job=job.as_dict())


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job introuvable")
    return JSONResponse(job.as_dict())
