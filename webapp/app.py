"""
UI web GEOeval — FastAPI + Jinja2 + DSFR.

Lancement :
    uvicorn webapp.app:app --reload
ou :
    python run_web.py

Depuis PR#12 (ADR-077) les routes de domaine sont préfixées `/o/{org_slug}/…`.
Les anciennes URL (`/runs`, `/dashboard`, …) redirigent en 301 vers l'org
primaire du user courant, ce qui préserve la compat des liens externes.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from db import SessionLocal
from load import load_tests
from webapp import audit, scheduler, services, tenancy
from webapp.auth import CurrentUser, GateAuthMiddleware
from webapp.deps import (
    get_db,
    require_org,
    require_platform_admin,
    require_role,
    require_user,
)
from webapp.jobs import manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logging.getLogger().setLevel(logging.INFO)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="GEOeval")
app.add_middleware(GateAuthMiddleware)
scheduler.start()


def _opt_int(value: Optional[str]) -> Optional[int]:
    if value is None or value.strip() == "":
        return None
    return int(value)


def render(
    request: Request,
    template: str,
    active: str,
    org=None,
    role: Optional[str] = None,
    **ctx,
) -> HTMLResponse:
    """Rendu Jinja + injection tenancy commune.

    org/role passés ici sont propagés au template via `url_prefix` et un dict
    `org` (id, name, slug) pour la nav DSFR.
    """
    user: Optional[CurrentUser] = getattr(request.state, "user", None)
    context = {
        "active": active,
        "user": user,
        "org": {"id": org.id, "name": org.name, "slug": org.slug} if org is not None else None,
        "role": role,
        "url_prefix": f"/o/{org.slug}" if org is not None else "",
        **ctx,
    }
    return templates.TemplateResponse(request=request, name=template, context=context)


# =====================================================================
# Landing (choix d'organisation)
# =====================================================================
@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    user: Optional[CurrentUser] = getattr(request.state, "user", None)
    # Pas de user (headers gate manquants et pas de DEV_FAKE_EMAIL) :
    # on affiche une page d'accueil neutre expliquant la situation.
    if user is None:
        return templates.TemplateResponse(
            request=request,
            name="landing_anonymous.html",
            context={"active": "home"},
        )

    if user.is_platform_admin:
        orgs = tenancy.list_all_orgs(db)
    else:
        orgs = tenancy.list_orgs_for_user(db, user.id)

    # Un seul choix : redirige direct sur son dashboard.
    if len(orgs) == 1:
        return RedirectResponse(f"/o/{orgs[0].slug}/", status_code=302)

    return templates.TemplateResponse(
        request=request,
        name="landing.html",
        context={
            "active": "home",
            "user": user,
            "orgs": orgs,
            "url_prefix": "",
        },
    )


# =====================================================================
# Aliases 301 depuis les anciennes URL vers l'org primaire du user courant.
# Préserve les liens externes (bookmark, doc, mail) qui pointent /runs, etc.
# =====================================================================
_LEGACY_PATHS = (
    "dashboard",
    "runs",
    "tests",
    "prompts",
    "models",
    "launch",
    "schedules",
    "jobs",
)


def _primary_org_slug(request: Request, db: Session) -> Optional[str]:
    user: Optional[CurrentUser] = getattr(request.state, "user", None)
    if user is None:
        return None
    if user.memberships_by_slug:
        return next(iter(user.memberships_by_slug))
    if user.is_platform_admin:
        orgs = tenancy.list_all_orgs(db)
        if orgs:
            return orgs[0].slug
    return None


def _install_legacy_redirect(path: str) -> None:
    @app.get(f"/{path}")
    def _redir(request: Request, db: Session = Depends(get_db)):  # noqa: ANN001
        slug = _primary_org_slug(request, db)
        if slug is None:
            return RedirectResponse("/", status_code=302)
        return RedirectResponse(f"/o/{slug}/{path}", status_code=301)


for _p in _LEGACY_PATHS:
    _install_legacy_redirect(_p)


# =====================================================================
# Dashboard (par org)
# =====================================================================
@app.get("/o/{org_slug}/", response_class=HTMLResponse)
def org_home(request: Request, ctx=Depends(require_org), db: Session = Depends(get_db)):
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


@app.get("/o/{org_slug}/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, ctx=Depends(require_org), db: Session = Depends(get_db)):
    org, role = ctx
    return render(
        request,
        "dashboard.html",
        active="dashboard",
        org=org,
        role=role,
        leaderboard=services.leaderboard(db, org.id),
        runs=services.list_runs(db, org.id)[:5],
    )


# =====================================================================
# Runs (par org)
# =====================================================================
@app.get("/o/{org_slug}/runs", response_class=HTMLResponse)
def runs(request: Request, ctx=Depends(require_org), db: Session = Depends(get_db)):
    org, role = ctx
    return render(
        request, "runs.html", active="runs", org=org, role=role,
        runs=services.list_runs(db, org.id),
    )


@app.get("/o/{org_slug}/runs/{run_id}", response_class=HTMLResponse)
def run_detail(run_id: int, request: Request, ctx=Depends(require_org), db: Session = Depends(get_db)):
    org, role = ctx
    detail = services.get_run_detail(db, org.id, run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} introuvable")
    return render(request, "run_detail.html", active="runs", org=org, role=role, run=detail)


# =====================================================================
# Tests (par org — édition = editor+)
# =====================================================================
@app.get("/o/{org_slug}/tests", response_class=HTMLResponse)
def tests(request: Request, ctx=Depends(require_org), db: Session = Depends(get_db)):
    org, role = ctx
    return render(
        request, "tests.html", active="tests", org=org, role=role,
        tests=services.list_tests(db, org.id),
    )


@app.get("/o/{org_slug}/tests/new", response_class=HTMLResponse)
def test_new(request: Request, ctx=Depends(require_role("editor")), db: Session = Depends(get_db)):
    org, role = ctx
    return render(
        request,
        "test_form.html",
        active="tests",
        org=org,
        role=role,
        test=None,
        prompts=services.list_prompts(db),
    )


@app.post("/o/{org_slug}/tests/new")
def test_create(
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
    prompt: str = Form(...),
    expected_answer: str = Form(""),
    response_quality_prompt_id: str = Form(""),
    citation_quality_prompt_id: str = Form(""),
):
    org, _ = ctx
    services.create_test(
        db,
        org.id,
        prompt=prompt,
        expected_answer=expected_answer,
        response_quality_prompt_id=_opt_int(response_quality_prompt_id),
        citation_quality_prompt_id=_opt_int(citation_quality_prompt_id),
    )
    return RedirectResponse(f"/o/{org.slug}/tests", status_code=303)


@app.get("/o/{org_slug}/tests/{test_id}/edit", response_class=HTMLResponse)
def test_edit(test_id: int, request: Request, ctx=Depends(require_role("editor")), db: Session = Depends(get_db)):
    org, role = ctx
    test = services.get_test(db, org.id, test_id)
    if test is None:
        raise HTTPException(status_code=404, detail=f"Test {test_id} introuvable")
    return render(
        request,
        "test_form.html",
        active="tests",
        org=org,
        role=role,
        test=test,
        prompts=services.list_prompts(db),
    )


@app.post("/o/{org_slug}/tests/{test_id}/edit")
def test_update(
    test_id: int,
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
    prompt: str = Form(...),
    expected_answer: str = Form(""),
    response_quality_prompt_id: str = Form(""),
    citation_quality_prompt_id: str = Form(""),
):
    org, _ = ctx
    services.update_test(
        db,
        org.id,
        test_id,
        prompt=prompt,
        expected_answer=expected_answer,
        response_quality_prompt_id=_opt_int(response_quality_prompt_id),
        citation_quality_prompt_id=_opt_int(citation_quality_prompt_id),
    )
    return RedirectResponse(f"/o/{org.slug}/tests", status_code=303)


@app.post("/o/{org_slug}/tests/{test_id}/deactivate")
def test_deactivate(test_id: int, ctx=Depends(require_role("editor")), db: Session = Depends(get_db)):
    org, _ = ctx
    services.deactivate_test(db, org.id, test_id)
    return RedirectResponse(f"/o/{org.slug}/tests", status_code=303)


@app.post("/o/{org_slug}/tests/{test_id}/reactivate")
def test_reactivate(test_id: int, ctx=Depends(require_role("editor")), db: Session = Depends(get_db)):
    org, _ = ctx
    services.reactivate_test(db, org.id, test_id)
    return RedirectResponse(f"/o/{org.slug}/tests", status_code=303)


# =====================================================================
# Prompts d'évaluation (catalogue global — édition réservée platform_admin)
# =====================================================================
@app.get("/o/{org_slug}/prompts", response_class=HTMLResponse)
def prompts(request: Request, ctx=Depends(require_org), db: Session = Depends(get_db)):
    org, role = ctx
    return render(
        request, "prompts.html", active="prompts", org=org, role=role,
        prompts=services.list_prompts(db),
    )


@app.get("/o/{org_slug}/prompts/new", response_class=HTMLResponse)
def prompt_new(
    request: Request,
    ctx=Depends(require_org),
    _pa: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    org, role = ctx
    return render(
        request,
        "prompt_form.html",
        active="prompts",
        org=org,
        role=role,
        prompt=None,
        prompt_types=services.list_prompt_types(db),
    )


@app.post("/o/{org_slug}/prompts/new")
def prompt_create(
    ctx=Depends(require_org),
    _pa: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
    prompt_type_id: int = Form(...),
    prompt_name: str = Form(...),
    prompt_text: str = Form(...),
):
    org, _ = ctx
    services.create_prompt(
        db, prompt_type_id=prompt_type_id, prompt_name=prompt_name, prompt_text=prompt_text
    )
    return RedirectResponse(f"/o/{org.slug}/prompts", status_code=303)


@app.get("/o/{org_slug}/prompts/{prompt_id}/edit", response_class=HTMLResponse)
def prompt_edit(
    prompt_id: int,
    request: Request,
    ctx=Depends(require_org),
    _pa: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    org, role = ctx
    prompt = services.get_prompt(db, prompt_id)
    if prompt is None:
        raise HTTPException(status_code=404, detail=f"Prompt {prompt_id} introuvable")
    return render(
        request,
        "prompt_form.html",
        active="prompts",
        org=org,
        role=role,
        prompt=prompt,
        prompt_types=services.list_prompt_types(db),
    )


@app.post("/o/{org_slug}/prompts/{prompt_id}/edit")
def prompt_update(
    prompt_id: int,
    ctx=Depends(require_org),
    _pa: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
    prompt_type_id: int = Form(...),
    prompt_name: str = Form(...),
    prompt_text: str = Form(...),
):
    org, _ = ctx
    services.update_prompt(
        db,
        prompt_id,
        prompt_type_id=prompt_type_id,
        prompt_name=prompt_name,
        prompt_text=prompt_text,
    )
    return RedirectResponse(f"/o/{org.slug}/prompts", status_code=303)


# =====================================================================
# Lancer un run (par org)
# =====================================================================
# Providers utilisables comme modèle TESTÉ (dispatch de run.py, avec recherche web).
TESTABLE_PROVIDERS = {"openai", "chatgpt", "gpt", "mistral", "mistralai", "gemini", "google"}
PROVIDER_CHOICES = ["chatGPT", "mistral", "gemini", "albert", "compatible-openai"]


def _run_form_context(db: Session, org_id: int) -> dict:
    """Contexte commun aux formulaires « lancer » et « planifier » un run."""
    models = services.list_models(db)
    return dict(
        models=models,
        testable_models=[
            m for m in models if (m.model_name or "").lower() in TESTABLE_PROVIDERS
        ],
        judgeable_models=[m for m in models if m.is_judge],
        tests=load_tests(db, organization_id=org_id),
    )


def _parse_run_selection(
    db: Session,
    org_id: int,
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
    all_active_ids = [t.test_id for t in load_tests(db, organization_id=org_id)]
    if not all_active_ids:
        raise HTTPException(
            status_code=400,
            detail="Aucun test actif et prêt : active ou crée des tests avant de lancer un run.",
        )
    if not test_ids:
        raise HTTPException(status_code=400, detail="Sélectionne au moins un test.")
    selected: Optional[list[int]] = None if set(test_ids) >= set(all_active_ids) else test_ids
    judges = [{"model": v, "repeats": max(1, repeats)} for v in judge_models]
    return dict(tested_models=tested_models, judges=judges, test_ids=selected)


@app.get("/o/{org_slug}/launch", response_class=HTMLResponse)
def launch_form(request: Request, ctx=Depends(require_role("editor")), db: Session = Depends(get_db)):
    org, role = ctx
    fctx = _run_form_context(db, org.id)
    return render(request, "launch.html", active="launch", org=org, role=role,
                  active_tests_count=len(fctx["tests"]), **fctx)


@app.post("/o/{org_slug}/launch")
def launch_submit(
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
    tested_models: list[str] = Form(default=[]),
    judge_models: list[str] = Form(default=[]),
    repeats: int = Form(1),
    note: str = Form(""),
    test_ids: list[int] = Form(default=[]),
):
    org, _ = ctx
    params = _parse_run_selection(db, org.id, tested_models, judge_models, repeats, test_ids)
    job = manager.submit(dict(**params, note=note or None, organization_id=org.id))
    return RedirectResponse(f"/o/{org.slug}/jobs/{job.id}", status_code=303)


# =====================================================================
# Modèles (catalogue global — édition réservée platform_admin)
# =====================================================================
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


@app.get("/o/{org_slug}/models", response_class=HTMLResponse)
def models_list(request: Request, ctx=Depends(require_org), db: Session = Depends(get_db)):
    org, role = ctx
    models = services.list_models(db, active_only=False)
    refs = {m.model_id: services.model_run_refs(db, m.model_id) for m in models}
    return render(request, "models.html", active="models", org=org, role=role,
                  models=models, refs=refs)


@app.get("/o/{org_slug}/models/new", response_class=HTMLResponse)
def model_new_form(
    request: Request,
    ctx=Depends(require_org),
    _pa: CurrentUser = Depends(require_platform_admin),
):
    org, role = ctx
    return render(request, "model_form.html", active="models", org=org, role=role,
                  model=None, providers=PROVIDER_CHOICES)


@app.post("/o/{org_slug}/models/new")
def model_new_submit(
    ctx=Depends(require_org),
    _pa: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
    model_name: str = Form(...),
    model_version: str = Form(...),
    base_url: str = Form(""),
    api_key: str = Form(""),
    extra_headers: str = Form(""),
):
    org, _ = ctx
    services.create_model(
        db,
        model_name=model_name.strip(),
        model_version=model_version.strip(),
        base_url=base_url.strip(),
        api_key=api_key.strip(),
        extra_headers=_parse_headers(extra_headers),
    )
    return RedirectResponse(f"/o/{org.slug}/models", status_code=303)


@app.get("/o/{org_slug}/models/{model_id}/edit", response_class=HTMLResponse)
def model_edit_form(
    model_id: int,
    request: Request,
    ctx=Depends(require_org),
    _pa: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    org, role = ctx
    model = services.get_model(db, model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="Modèle introuvable.")
    return render(request, "model_form.html", active="models", org=org, role=role,
                  model=model, providers=PROVIDER_CHOICES)


@app.post("/o/{org_slug}/models/{model_id}/edit")
def model_edit_submit(
    model_id: int,
    ctx=Depends(require_org),
    _pa: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
    model_name: str = Form(...),
    model_version: str = Form(...),
    base_url: str = Form(""),
    api_key: str = Form(""),
    clear_api_key: bool = Form(False),
    extra_headers: str = Form(""),
):
    org, _ = ctx
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
    return RedirectResponse(f"/o/{org.slug}/models", status_code=303)


@app.post("/o/{org_slug}/models/{model_id}/toggle-judge")
def model_toggle_judge(
    model_id: int,
    ctx=Depends(require_org),
    _pa: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    org, _ = ctx
    services.toggle_model_judge(db, model_id)
    return RedirectResponse(f"/o/{org.slug}/models", status_code=303)


@app.post("/o/{org_slug}/models/{model_id}/deactivate")
def model_deactivate(
    model_id: int,
    ctx=Depends(require_org),
    _pa: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    org, _ = ctx
    services.set_model_active(db, model_id, False)
    return RedirectResponse(f"/o/{org.slug}/models", status_code=303)


@app.post("/o/{org_slug}/models/{model_id}/reactivate")
def model_reactivate(
    model_id: int,
    ctx=Depends(require_org),
    _pa: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    org, _ = ctx
    services.set_model_active(db, model_id, True)
    return RedirectResponse(f"/o/{org.slug}/models", status_code=303)


@app.post("/o/{org_slug}/models/{model_id}/delete")
def model_delete(
    model_id: int,
    ctx=Depends(require_org),
    _pa: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    org, _ = ctx
    try:
        services.delete_model(db, model_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return RedirectResponse(f"/o/{org.slug}/models", status_code=303)


# =====================================================================
# Runs programmés (par org)
# =====================================================================
@app.get("/o/{org_slug}/schedules", response_class=HTMLResponse)
def schedules_list(request: Request, ctx=Depends(require_org), db: Session = Depends(get_db)):
    org, role = ctx
    schedules = services.list_schedules(db, org.id)
    return render(
        request, "schedules.html", active="schedules", org=org, role=role,
        schedules=schedules, describe=scheduler.describe_schedule, tz=scheduler.TZ_PARIS,
    )


@app.get("/o/{org_slug}/schedules/new", response_class=HTMLResponse)
def schedule_new_form(request: Request, ctx=Depends(require_role("editor")), db: Session = Depends(get_db)):
    org, role = ctx
    fctx = _run_form_context(db, org.id)
    return render(request, "schedule_form.html", active="schedules", org=org, role=role,
                  weekdays=scheduler.WEEKDAYS_FR, **fctx)


@app.post("/o/{org_slug}/schedules/new")
def schedule_new_submit(
    ctx=Depends(require_role("editor")),
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
    org, _ = ctx
    params = _parse_run_selection(db, org.id, tested_models, judge_models, repeats, test_ids)

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
        org.id,
        name=name.strip(),
        tested_models=params["tested_models"],
        judges=params["judges"],
        test_ids=params["test_ids"],
        note=note or None,
        schedule_kind=schedule_kind,
        schedule_config=config,
        next_run_at=next_run,
    )
    return RedirectResponse(f"/o/{org.slug}/schedules", status_code=303)


@app.post("/o/{org_slug}/schedules/{schedule_id}/toggle")
def schedule_toggle(
    schedule_id: int,
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
):
    org, _ = ctx
    sr = services.get_schedule(db, org.id, schedule_id)
    if sr is None:
        raise HTTPException(status_code=404, detail="Planification introuvable.")
    if sr.enabled:
        services.set_schedule_enabled(db, org.id, schedule_id, False)
    else:
        next_run = scheduler.compute_next_run(sr.schedule_kind, sr.schedule_config)
        if next_run is None:
            raise HTTPException(
                status_code=400,
                detail="Impossible de réactiver : la date one-shot est passée. Crée une nouvelle planification.",
            )
        services.set_schedule_enabled(db, org.id, schedule_id, True, next_run_at=next_run)
    return RedirectResponse(f"/o/{org.slug}/schedules", status_code=303)


@app.post("/o/{org_slug}/schedules/{schedule_id}/run-now")
def schedule_run_now(
    schedule_id: int,
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
):
    org, _ = ctx
    sr = services.get_schedule(db, org.id, schedule_id)
    if sr is None:
        raise HTTPException(status_code=404, detail="Planification introuvable.")
    job = manager.submit(
        dict(
            organization_id=org.id,
            tested_models=list(sr.tested_models),
            judges=list(sr.judges),
            note=sr.note or f"planifié : {sr.name} (manuel)",
            test_ids=list(sr.test_ids) if sr.test_ids else None,
        )
    )
    return RedirectResponse(f"/o/{org.slug}/jobs/{job.id}", status_code=303)


@app.post("/o/{org_slug}/schedules/{schedule_id}/delete")
def schedule_delete(
    schedule_id: int,
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
):
    org, _ = ctx
    services.delete_schedule(db, org.id, schedule_id)
    return RedirectResponse(f"/o/{org.slug}/schedules", status_code=303)


# =====================================================================
# Jobs (par org — un job carry son organization_id ; isolation vérifiée)
# =====================================================================
def _org_jobs(org_id: int):
    return [j for j in manager.list() if int(j.params.get("organization_id", -1)) == org_id]


@app.get("/o/{org_slug}/jobs", response_class=HTMLResponse)
def jobs_list(request: Request, ctx=Depends(require_org)):
    org, role = ctx
    return render(request, "jobs.html", active="launch", org=org, role=role,
                  jobs=[j.as_dict() for j in _org_jobs(org.id)])


@app.get("/o/{org_slug}/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(job_id: str, request: Request, ctx=Depends(require_org)):
    org, role = ctx
    job = manager.get(job_id)
    if job is None or int(job.params.get("organization_id", -1)) != org.id:
        raise HTTPException(status_code=404, detail=f"Job {job_id} introuvable")
    return render(request, "job_detail.html", active="launch", org=org, role=role,
                  job=job.as_dict())


@app.get("/o/{org_slug}/api/jobs/{job_id}")
def job_status(job_id: str, ctx=Depends(require_org)):
    org, _ = ctx
    job = manager.get(job_id)
    if job is None or int(job.params.get("organization_id", -1)) != org.id:
        raise HTTPException(status_code=404, detail="job introuvable")
    return JSONResponse(job.as_dict())


# =====================================================================
# PR#13 — Paramètres d'organisation, membres, invitations, audit
# =====================================================================
from sqlalchemy import select as _sel  # noqa: E402
from models import AuditLog as _AuditLog, User as _User  # noqa: E402


@app.get("/o/{org_slug}/settings", response_class=HTMLResponse)
def org_settings(
    request: Request,
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
):
    org, role = ctx
    return render(
        request,
        "org_settings.html",
        active="settings",
        org=org,
        role=role,
        members=tenancy.list_members(db, org.id),
        invitations=tenancy.list_invitations(db, org.id),
        roles=tenancy.ROLES,
    )


@app.post("/o/{org_slug}/settings/rename")
def org_rename(
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
    name: str = Form(...),
):
    org, _ = ctx
    old = org.name
    tenancy.rename_org(db, org.id, name=name)
    audit.record(
        db,
        user_id=user.id,
        org_id=org.id,
        action="rename",
        entity_type="organization",
        entity_id=org.id,
        meta={"old": old, "new": name},
    )
    return RedirectResponse(f"/o/{org.slug}/settings", status_code=303)


@app.post("/o/{org_slug}/members/{user_id}/role")
def member_change_role(
    user_id: int,
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
    role: str = Form(...),
):
    org, _ = ctx
    try:
        tenancy.set_membership_role(db, user_id=user_id, org_id=org.id, role=role)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit.record(
        db,
        user_id=user.id,
        org_id=org.id,
        action="set_role",
        entity_type="membership",
        entity_id=user_id,
        meta={"role": role},
    )
    return RedirectResponse(f"/o/{org.slug}/settings", status_code=303)


@app.post("/o/{org_slug}/members/{user_id}/remove")
def member_remove(
    user_id: int,
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
):
    org, _ = ctx
    if user_id == user.id:
        raise HTTPException(status_code=400, detail="Impossible de te retirer toi-même.")
    tenancy.remove_membership(db, user_id=user_id, org_id=org.id)
    audit.record(
        db,
        user_id=user.id,
        org_id=org.id,
        action="remove",
        entity_type="membership",
        entity_id=user_id,
    )
    return RedirectResponse(f"/o/{org.slug}/settings", status_code=303)


@app.post("/o/{org_slug}/invitations")
def invitation_create(
    request: Request,
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
    email: str = Form(...),
    role: str = Form(...),
):
    org, _ = ctx
    if role not in tenancy.ROLES:
        raise HTTPException(status_code=400, detail=f"Rôle invalide : {role!r}.")
    inv = tenancy.create_invitation(
        db, org_id=org.id, email=email, role=role, invited_by=user.id
    )
    audit.record(
        db,
        user_id=user.id,
        org_id=org.id,
        action="create",
        entity_type="invitation",
        entity_id=inv.id,
        meta={"email": inv.email, "role": role},
    )
    return RedirectResponse(f"/o/{org.slug}/settings", status_code=303)


@app.post("/o/{org_slug}/invitations/{inv_id}/revoke")
def invitation_revoke(
    inv_id: int,
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
):
    org, _ = ctx
    try:
        tenancy.revoke_invitation(db, org.id, inv_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit.record(
        db,
        user_id=user.id,
        org_id=org.id,
        action="revoke",
        entity_type="invitation",
        entity_id=inv_id,
    )
    return RedirectResponse(f"/o/{org.slug}/settings", status_code=303)


@app.get("/o/{org_slug}/accept-invite", response_class=HTMLResponse)
def accept_invite_form(
    org_slug: str,
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
    token: str = "",
):
    """Landing d'acceptation d'invitation.

    Volontairement PAS derrière `require_org` : un invité qui n'est pas encore
    membre doit pouvoir atterrir sur cette page (sinon 404 avant qu'il puisse
    confirmer). L'org est résolue à la volée, sans vérif de membership.
    """
    org = tenancy.get_org_by_slug(db, org_slug)
    if org is None:
        raise HTTPException(status_code=404, detail="Organisation introuvable.")
    return render(
        request,
        "accept_invite.html",
        active="settings",
        org=org,
        role=None,
        token=token,
        current_email=user.email,
    )


@app.post("/o/{org_slug}/accept-invite")
def accept_invite_submit(
    org_slug: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
    token: str = Form(...),
):
    org = tenancy.get_org_by_slug(db, org_slug)
    if org is None:
        raise HTTPException(status_code=404, detail="Organisation introuvable.")
    try:
        m = tenancy.accept_invitation(db, token=token, current_email=user.email)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    if m.org_id != org.id:
        raise HTTPException(status_code=400, detail="Token pour une autre organisation.")
    audit.record(
        db,
        user_id=user.id,
        org_id=org.id,
        action="accept",
        entity_type="invitation",
        entity_id=None,
        meta={"role": m.role},
    )
    return RedirectResponse(f"/o/{org.slug}/", status_code=303)


@app.get("/o/{org_slug}/audit", response_class=HTMLResponse)
def org_audit_view(
    request: Request,
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
    page: int = 0,
):
    org, role = ctx
    PAGE_SIZE = 50
    rows = db.execute(
        _sel(_AuditLog, _User.email)
        .outerjoin(_User, _User.id == _AuditLog.user_id)
        .where(_AuditLog.org_id == org.id)
        .order_by(_AuditLog.at.desc())
        .offset(page * PAGE_SIZE)
        .limit(PAGE_SIZE)
    ).all()
    entries = [
        dict(
            id=al.id, at=al.at, action=al.action, entity_type=al.entity_type,
            entity_id=al.entity_id, meta=al.meta_json, actor=email or "—",
        )
        for al, email in rows
    ]
    return render(
        request, "audit.html", active="settings", org=org, role=role,
        entries=entries, page=page, next_page=page + 1 if len(entries) == PAGE_SIZE else None,
    )


# =====================================================================
# Admin plateforme — création d'org, audit global
# =====================================================================
@app.get("/admin/organizations", response_class=HTMLResponse)
def admin_orgs(
    request: Request,
    user: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    orgs = tenancy.list_all_orgs(db)
    return templates.TemplateResponse(
        request=request,
        name="admin_organizations.html",
        context={
            "active": "admin",
            "user": user,
            "url_prefix": "",
            "orgs": orgs,
            "roles": tenancy.ROLES,
        },
    )


@app.post("/admin/organizations/new")
def admin_org_create(
    user: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
    name: str = Form(...),
    slug: str = Form(...),
    first_admin_email: str = Form(""),
):
    try:
        org = tenancy.create_org(db, name=name, slug=slug, created_by=user.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    audit.record(
        db,
        user_id=user.id,
        org_id=org.id,
        action="create",
        entity_type="organization",
        entity_id=org.id,
        meta={"slug": org.slug},
    )

    # Premier admin optionnel : pose ou crée l'user + son membership org_admin.
    first_admin_email = (first_admin_email or "").strip().lower()
    if first_admin_email:
        from webapp.auth import load_or_provision_user  # local import

        with SessionLocal() as bs:
            cu = load_or_provision_user(bs, first_admin_email, groups=[])
            try:
                tenancy.add_membership(bs, user_id=cu.id, org_id=org.id, role="org_admin")
            except Exception:
                pass  # déjà membre — idempotent
        audit.record(
            db,
            user_id=user.id,
            org_id=org.id,
            action="add_admin",
            entity_type="membership",
            entity_id=None,
            meta={"email": first_admin_email},
        )
    return RedirectResponse("/admin/organizations", status_code=303)


@app.get("/admin/audit", response_class=HTMLResponse)
def admin_audit_view(
    request: Request,
    user: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
    page: int = 0,
):
    from models import Organization  # local — cycle-safe
    PAGE_SIZE = 50
    rows = db.execute(
        _sel(_AuditLog, _User.email, Organization.slug)
        .outerjoin(_User, _User.id == _AuditLog.user_id)
        .outerjoin(Organization, Organization.id == _AuditLog.org_id)
        .order_by(_AuditLog.at.desc())
        .offset(page * PAGE_SIZE)
        .limit(PAGE_SIZE)
    ).all()
    entries = [
        dict(
            id=al.id, at=al.at, action=al.action, entity_type=al.entity_type,
            entity_id=al.entity_id, meta=al.meta_json, actor=email or "—",
            org_slug=slug or "—",
        )
        for al, email, slug in rows
    ]
    return templates.TemplateResponse(
        request=request,
        name="admin_audit.html",
        context={
            "active": "admin",
            "user": user,
            "url_prefix": "",
            "entries": entries,
            "page": page,
            "next_page": page + 1 if len(entries) == PAGE_SIZE else None,
        },
    )
