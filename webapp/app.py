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

import json

from db import SessionLocal
from load import load_tests
from webapp import scheduler, services
from webapp.jobs import manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logging.getLogger().setLevel(logging.INFO)  # au cas où l'entrypoint a déjà configuré le root

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="GEOeval")
scheduler.start()


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
# Accueil (présentation)
# -----------------------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    return render(
        request,
        "home.html",
        active="home",
        n_models=len(services.list_models(db)),
        n_tests=len(load_tests(db)),
        n_runs=len(services.list_runs(db)),
    )


# -----------------------------
# Tableau de bord
# -----------------------------
@app.get("/dashboard", response_class=HTMLResponse)
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
# Providers utilisables comme modèle TESTÉ (= dispatch de run.py, avec recherche
# web). Les autres (albert, compatible-openai…) ne peuvent servir que de juge.
TESTABLE_PROVIDERS = {"openai", "chatgpt", "gpt", "mistral", "mistralai", "gemini", "google"}

# Providers proposés dans la page Modèles (pilotent le dispatch du code).
PROVIDER_CHOICES = ["chatGPT", "mistral", "gemini", "albert", "compatible-openai"]


def _run_form_context(db: Session) -> dict:
    """Contexte commun aux formulaires « lancer » et « planifier » un run."""
    models = services.list_models(db)
    return dict(
        models=models,
        testable_models=[
            m for m in models if (m.model_name or "").lower() in TESTABLE_PROVIDERS
        ],
        judgeable_models=[m for m in models if m.is_judge],
        tests=load_tests(db),
    )


def _parse_run_selection(
    db: Session,
    tested_models: list[str],
    judge_models: list[str],
    repeats: int,
    test_ids: list[int],
) -> dict:
    """Valide la sélection commune (modèles, juges, tests) et renvoie les params job."""
    if not tested_models or not judge_models:
        raise HTTPException(
            status_code=400,
            detail="Sélectionne au moins un modèle testé et un juge.",
        )
    all_active_ids = [t.test_id for t in load_tests(db)]
    if not all_active_ids:
        raise HTTPException(
            status_code=400,
            detail="Aucun test actif et prêt : active ou crée des tests avant de lancer un run.",
        )
    if not test_ids:
        raise HTTPException(status_code=400, detail="Sélectionne au moins un test.")
    # Tous les tests actifs cochés => None (la planification suivra les tests
    # créés/désactivés ensuite, au lieu de figer la liste).
    selected: Optional[list[int]] = None if set(test_ids) >= set(all_active_ids) else test_ids
    judges = [{"model": v, "repeats": max(1, repeats)} for v in judge_models]
    return dict(tested_models=tested_models, judges=judges, test_ids=selected)


@app.get("/launch", response_class=HTMLResponse)
def launch_form(request: Request, db: Session = Depends(get_db)):
    ctx = _run_form_context(db)
    return render(request, "launch.html", active="launch",
                  active_tests_count=len(ctx["tests"]), **ctx)


@app.post("/launch")
def launch_submit(
    db: Session = Depends(get_db),
    tested_models: list[str] = Form(default=[]),
    judge_models: list[str] = Form(default=[]),
    repeats: int = Form(1),
    note: str = Form(""),
    test_ids: list[int] = Form(default=[]),
):
    params = _parse_run_selection(db, tested_models, judge_models, repeats, test_ids)
    job = manager.submit(dict(**params, note=note or None))
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


# -----------------------------
# Modèles (catalogue + accès API)
# -----------------------------
def _parse_headers(raw: str) -> Optional[dict]:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"En-têtes HTTP : JSON invalide ({exc}).")
    if not isinstance(obj, dict) or not all(isinstance(v, str) for v in obj.values()):
        raise HTTPException(
            status_code=400,
            detail='En-têtes HTTP : objet JSON attendu, valeurs texte (ex. {"X-Api-Version": "2"}).',
        )
    return obj


@app.get("/models", response_class=HTMLResponse)
def models_list(request: Request, db: Session = Depends(get_db)):
    models = services.list_models(db, active_only=False)
    refs = {m.model_id: services.model_run_refs(db, m.model_id) for m in models}
    return render(request, "models.html", active="models", models=models, refs=refs)


@app.get("/models/new", response_class=HTMLResponse)
def model_new_form(request: Request):
    return render(request, "model_form.html", active="models",
                  model=None, providers=PROVIDER_CHOICES)


@app.post("/models/new")
def model_new_submit(
    db: Session = Depends(get_db),
    model_name: str = Form(...),
    model_version: str = Form(...),
    base_url: str = Form(""),
    api_key: str = Form(""),
    extra_headers: str = Form(""),
):
    services.create_model(
        db,
        model_name=model_name.strip(),
        model_version=model_version.strip(),
        base_url=base_url.strip(),
        api_key=api_key.strip(),
        extra_headers=_parse_headers(extra_headers),
    )
    return RedirectResponse("/models", status_code=303)


@app.get("/models/{model_id}/edit", response_class=HTMLResponse)
def model_edit_form(model_id: int, request: Request, db: Session = Depends(get_db)):
    model = services.get_model(db, model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="Modèle introuvable.")
    return render(request, "model_form.html", active="models",
                  model=model, providers=PROVIDER_CHOICES)


@app.post("/models/{model_id}/edit")
def model_edit_submit(
    model_id: int,
    db: Session = Depends(get_db),
    model_name: str = Form(...),
    model_version: str = Form(...),
    base_url: str = Form(""),
    api_key: str = Form(""),
    clear_api_key: bool = Form(False),
    extra_headers: str = Form(""),
):
    services.update_model(
        db,
        model_id,
        model_name=model_name.strip(),
        model_version=model_version.strip(),
        base_url=base_url.strip(),
        api_key=api_key.strip(),
        clear_api_key=clear_api_key,
        extra_headers=_parse_headers(extra_headers),
    )
    return RedirectResponse("/models", status_code=303)


@app.post("/models/{model_id}/toggle-judge")
def model_toggle_judge(model_id: int, db: Session = Depends(get_db)):
    services.toggle_model_judge(db, model_id)
    return RedirectResponse("/models", status_code=303)


@app.post("/models/{model_id}/deactivate")
def model_deactivate(model_id: int, db: Session = Depends(get_db)):
    services.set_model_active(db, model_id, False)
    return RedirectResponse("/models", status_code=303)


@app.post("/models/{model_id}/reactivate")
def model_reactivate(model_id: int, db: Session = Depends(get_db)):
    services.set_model_active(db, model_id, True)
    return RedirectResponse("/models", status_code=303)


@app.post("/models/{model_id}/delete")
def model_delete(model_id: int, db: Session = Depends(get_db)):
    try:
        services.delete_model(db, model_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return RedirectResponse("/models", status_code=303)


# -----------------------------
# Runs programmés
# -----------------------------
@app.get("/schedules", response_class=HTMLResponse)
def schedules_list(request: Request, db: Session = Depends(get_db)):
    schedules = services.list_schedules(db)
    return render(
        request, "schedules.html", active="schedules",
        schedules=schedules, describe=scheduler.describe_schedule, tz=scheduler.TZ_PARIS,
    )


@app.get("/schedules/new", response_class=HTMLResponse)
def schedule_new_form(request: Request, db: Session = Depends(get_db)):
    ctx = _run_form_context(db)
    return render(request, "schedule_form.html", active="schedules",
                  weekdays=scheduler.WEEKDAYS_FR, **ctx)


@app.post("/schedules/new")
def schedule_new_submit(
    db: Session = Depends(get_db),
    name: str = Form(...),
    tested_models: list[str] = Form(default=[]),
    judge_models: list[str] = Form(default=[]),
    repeats: int = Form(1),
    note: str = Form(""),
    test_ids: list[int] = Form(default=[]),
    schedule_kind: str = Form(...),
    once_at: str = Form(""),
    daily_time: str = Form(""),
    weekly_weekday: int = Form(0),
    weekly_time: str = Form(""),
    every_hours: int = Form(24),
):
    params = _parse_run_selection(db, tested_models, judge_models, repeats, test_ids)

    if schedule_kind == "once":
        if not once_at:
            raise HTTPException(status_code=400, detail="Indique la date et l'heure d'exécution.")
        config = {"at": once_at}
    elif schedule_kind == "daily":
        if not daily_time:
            raise HTTPException(status_code=400, detail="Indique l'heure quotidienne.")
        config = {"time": daily_time}
    elif schedule_kind == "weekly":
        if not weekly_time:
            raise HTTPException(status_code=400, detail="Indique le jour et l'heure hebdomadaires.")
        config = {"weekday": weekly_weekday, "time": weekly_time}
    elif schedule_kind == "every_n_hours":
        if every_hours < 1:
            raise HTTPException(status_code=400, detail="L'intervalle doit être d'au moins 1 heure.")
        config = {"hours": every_hours}
    else:
        raise HTTPException(status_code=400, detail=f"Type de planification inconnu : {schedule_kind!r}.")

    next_run = scheduler.compute_next_run(schedule_kind, config)
    if next_run is None:
        raise HTTPException(status_code=400, detail="La date d'exécution est déjà passée.")

    services.create_schedule(
        db,
        name=name.strip(),
        tested_models=params["tested_models"],
        judges=params["judges"],
        test_ids=params["test_ids"],
        note=note or None,
        schedule_kind=schedule_kind,
        schedule_config=config,
        next_run_at=next_run,
    )
    return RedirectResponse("/schedules", status_code=303)


@app.post("/schedules/{schedule_id}/toggle")
def schedule_toggle(schedule_id: int, db: Session = Depends(get_db)):
    sr = services.get_schedule(db, schedule_id)
    if sr is None:
        raise HTTPException(status_code=404, detail="Planification introuvable.")
    if sr.enabled:
        services.set_schedule_enabled(db, schedule_id, False)
    else:
        next_run = scheduler.compute_next_run(sr.schedule_kind, sr.schedule_config)
        if next_run is None:
            raise HTTPException(
                status_code=400,
                detail="Impossible de réactiver : la date one-shot est passée. Crée une nouvelle planification.",
            )
        services.set_schedule_enabled(db, schedule_id, True, next_run_at=next_run)
    return RedirectResponse("/schedules", status_code=303)


@app.post("/schedules/{schedule_id}/run-now")
def schedule_run_now(schedule_id: int, db: Session = Depends(get_db)):
    sr = services.get_schedule(db, schedule_id)
    if sr is None:
        raise HTTPException(status_code=404, detail="Planification introuvable.")
    job = manager.submit(
        dict(
            tested_models=list(sr.tested_models),
            judges=list(sr.judges),
            note=sr.note or f"planifié : {sr.name} (manuel)",
            test_ids=list(sr.test_ids) if sr.test_ids else None,
        )
    )
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@app.post("/schedules/{schedule_id}/delete")
def schedule_delete(schedule_id: int, db: Session = Depends(get_db)):
    services.delete_schedule(db, schedule_id)
    return RedirectResponse("/schedules", status_code=303)


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
