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
from webapp import (
    agreement,
    audit,
    budget,
    credentials,
    gold,
    ground_truth,
    openrouter_catalog,
    perimeters,
    pricing,
    scheduler,
    services,
    tenancy,
)
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
# Périmètres (PR#18) — objet intermédiaire org → questions
# =====================================================================
@app.get("/o/{org_slug}/perimeters", response_class=HTMLResponse)
def perimeters_list(request: Request, ctx=Depends(require_org), db: Session = Depends(get_db)):
    org, role = ctx
    plist = perimeters.list_for_org(db, org.id)
    counts = {p.id: perimeters.count_tests(db, p.id) for p in plist}
    return render(
        request, "perimeters.html", active="perimeters", org=org, role=role,
        perimeters=plist, counts=counts,
    )


@app.get("/o/{org_slug}/perimeters/new", response_class=HTMLResponse)
def perimeter_new_form(
    request: Request, ctx=Depends(require_role("editor")), db: Session = Depends(get_db)
):
    org, role = ctx
    return render(
        request, "perimeter_form.html", active="perimeters", org=org, role=role,
        perimeter=None,
    )


@app.post("/o/{org_slug}/perimeters/new")
def perimeter_create(
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
    name: str = Form(...),
    slug: str = Form(...),
    kind: str = Form(""),
    home_url: str = Form(""),
    description: str = Form(""),
):
    org, _ = ctx
    try:
        p = perimeters.create(
            db, org_id=org.id,
            name=name, slug=slug, kind=kind or None,
            home_url=home_url or None, description=description or None,
            created_by=user.id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit.record(
        db, user_id=user.id, org_id=org.id,
        action="create", entity_type="perimeter", entity_id=p.id,
        meta={"slug": p.slug, "name": p.name},
    )
    return RedirectResponse(f"/o/{org.slug}/perimeters/{p.id}", status_code=303)


@app.get("/o/{org_slug}/perimeters/{perimeter_id}", response_class=HTMLResponse)
def perimeter_detail(
    perimeter_id: int, request: Request,
    ctx=Depends(require_org), db: Session = Depends(get_db),
):
    org, role = ctx
    p = perimeters.get_by_id(db, org.id, perimeter_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Périmètre introuvable.")
    return render(
        request, "perimeter_detail.html", active="perimeters", org=org, role=role,
        perimeter=p,
        tests=services.list_tests(db, org.id, perimeter_id=p.id),
        all_perimeters=perimeters.list_for_org(db, org.id),
    )


@app.get("/o/{org_slug}/perimeters/{perimeter_id}/edit", response_class=HTMLResponse)
def perimeter_edit_form(
    perimeter_id: int, request: Request,
    ctx=Depends(require_role("editor")), db: Session = Depends(get_db),
):
    org, role = ctx
    p = perimeters.get_by_id(db, org.id, perimeter_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Périmètre introuvable.")
    return render(
        request, "perimeter_form.html", active="perimeters", org=org, role=role,
        perimeter=p,
    )


@app.post("/o/{org_slug}/perimeters/{perimeter_id}/edit")
def perimeter_edit_submit(
    perimeter_id: int,
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
    name: str = Form(...),
    kind: str = Form(""),
    home_url: str = Form(""),
    description: str = Form(""),
):
    org, _ = ctx
    try:
        perimeters.update(
            db, org_id=org.id, perimeter_id=perimeter_id,
            name=name, kind=kind or None,
            home_url=home_url or None, description=description or None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit.record(
        db, user_id=user.id, org_id=org.id,
        action="update", entity_type="perimeter", entity_id=perimeter_id,
    )
    return RedirectResponse(f"/o/{org.slug}/perimeters/{perimeter_id}", status_code=303)


@app.post("/o/{org_slug}/perimeters/{perimeter_id}/delete")
def perimeter_delete(
    perimeter_id: int,
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
):
    org, _ = ctx
    try:
        perimeters.delete(db, org.id, perimeter_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit.record(
        db, user_id=user.id, org_id=org.id,
        action="delete", entity_type="perimeter", entity_id=perimeter_id,
    )
    return RedirectResponse(f"/o/{org.slug}/perimeters", status_code=303)


# =====================================================================
# Tests (par org — édition = editor+)
# =====================================================================
@app.get("/o/{org_slug}/tests", response_class=HTMLResponse)
def tests(
    request: Request, ctx=Depends(require_org), db: Session = Depends(get_db),
    perimeter: str = "",
):
    org, role = ctx
    # Filtre optionnel par slug de périmètre.
    peri_id: Optional[int] = None
    peri_obj = None
    if perimeter:
        peri_obj = perimeters.get_by_slug(db, org.id, perimeter)
        peri_id = peri_obj.id if peri_obj else -1
    return render(
        request, "tests.html", active="tests", org=org, role=role,
        tests=services.list_tests(db, org.id, perimeter_id=peri_id),
        all_perimeters=perimeters.list_for_org(db, org.id),
        current_perimeter=peri_obj,
    )


@app.get("/o/{org_slug}/tests/new", response_class=HTMLResponse)
def test_new(
    request: Request,
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
    perimeter_id: int = 0,
):
    org, role = ctx
    default_peri = None
    if perimeter_id > 0:
        default_peri = perimeters.get_by_id(db, org.id, perimeter_id)
    return render(
        request, "test_form.html", active="tests", org=org, role=role,
        test=None,
        prompts=services.list_prompts(db),
        all_perimeters=perimeters.list_for_org(db, org.id),
        default_perimeter=default_peri,
    )


@app.post("/o/{org_slug}/tests/new")
def test_create(
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
    perimeter_id: int = Form(...),
    prompt: str = Form(...),
    expected_answer: str = Form(""),
    response_quality_prompt_id: str = Form(""),
    citation_quality_prompt_id: str = Form(""),
):
    org, _ = ctx
    peri = perimeters.get_by_id(db, org.id, perimeter_id)
    if peri is None:
        raise HTTPException(status_code=400, detail="Périmètre invalide.")
    services.create_test(
        db, org.id, perimeter_id=perimeter_id,
        prompt=prompt, expected_answer=expected_answer,
        response_quality_prompt_id=_opt_int(response_quality_prompt_id),
        citation_quality_prompt_id=_opt_int(citation_quality_prompt_id),
    )
    return RedirectResponse(f"/o/{org.slug}/perimeters/{perimeter_id}", status_code=303)


@app.get("/o/{org_slug}/tests/{test_id}/edit", response_class=HTMLResponse)
def test_edit(test_id: int, request: Request, ctx=Depends(require_role("editor")), db: Session = Depends(get_db)):
    org, role = ctx
    test = services.get_test(db, org.id, test_id)
    if test is None:
        raise HTTPException(status_code=404, detail=f"Test {test_id} introuvable")
    return render(
        request, "test_form.html", active="tests", org=org, role=role,
        test=test,
        prompts=services.list_prompts(db),
        all_perimeters=perimeters.list_for_org(db, org.id),
        default_perimeter=None,
    )


@app.post("/o/{org_slug}/tests/{test_id}/edit")
def test_update(
    test_id: int,
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
    perimeter_id: int = Form(...),
    prompt: str = Form(...),
    expected_answer: str = Form(""),
    response_quality_prompt_id: str = Form(""),
    citation_quality_prompt_id: str = Form(""),
):
    org, _ = ctx
    # Déplacement éventuel de périmètre.
    peri = perimeters.get_by_id(db, org.id, perimeter_id)
    if peri is None:
        raise HTTPException(status_code=400, detail="Périmètre invalide.")
    test = services.get_test(db, org.id, test_id)
    if test is None:
        raise HTTPException(status_code=404, detail="Test introuvable.")
    if test.perimeter_id != perimeter_id:
        test.perimeter_id = perimeter_id
        db.commit()
    services.update_test(
        db, org.id, test_id,
        prompt=prompt, expected_answer=expected_answer,
        response_quality_prompt_id=_opt_int(response_quality_prompt_id),
        citation_quality_prompt_id=_opt_int(citation_quality_prompt_id),
    )
    return RedirectResponse(f"/o/{org.slug}/perimeters/{perimeter_id}", status_code=303)


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


# ---- Vérité de référence (ADR-079 §1) --------------------------------
@app.get("/o/{org_slug}/tests/{test_id}", response_class=HTMLResponse)
def test_detail(
    test_id: int,
    request: Request,
    ctx=Depends(require_org),
    db: Session = Depends(get_db),
):
    org, role = ctx
    test = services.get_test(db, org.id, test_id)
    if test is None:
        raise HTTPException(status_code=404, detail=f"Test {test_id} introuvable")
    versions = ground_truth.list_versions(db, test_id)
    return render(
        request, "test_detail.html", active="tests", org=org, role=role,
        test=test, gt_versions=versions,
    )


@app.post("/o/{org_slug}/tests/{test_id}/ground-truth")
def test_ground_truth_create(
    test_id: int,
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
    reference_answer: str = Form(...),
    reference_urls: str = Form(""),
    notes: str = Form(""),
):
    org, _ = ctx
    test = services.get_test(db, org.id, test_id)
    if test is None:
        raise HTTPException(status_code=404, detail="Test introuvable")
    urls = [u.strip() for u in reference_urls.replace("\n", ",").split(",") if u.strip()]
    row = ground_truth.create_new_version(
        db,
        test_id=test_id,
        reference_answer=reference_answer,
        reference_urls=urls,
        created_by=user.id,
        notes=notes or None,
    )
    audit.record(
        db, user_id=user.id, org_id=org.id, action="create",
        entity_type="test_ground_truth", entity_id=row.id,
        meta={"test_id": test_id, "version": row.version},
    )
    return RedirectResponse(f"/o/{org.slug}/tests/{test_id}", status_code=303)


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
TESTABLE_PROVIDERS = {"openai", "chatgpt", "gpt", "mistral", "mistralai", "gemini", "google", "openrouter"}
PROVIDER_CHOICES = ["openrouter", "chatGPT", "mistral", "gemini", "albert", "compatible-openai"]


def _run_form_context(db: Session, org_id: int, perimeter_id: Optional[int] = None) -> dict:
    """Contexte commun aux formulaires « lancer » et « planifier » un run.

    Si `perimeter_id` est fourni, seules les questions de ce périmètre sont exposées.
    """
    models = services.list_models(db)
    judges = [m for m in models if m.is_judge]
    judge_kappa: dict[int, Optional[float]] = {}
    for j in judges:
        ag = agreement.compute_agreement_vs_gold(db, j.model_id)
        judge_kappa[j.model_id] = ag.get("response_kappa")
    all_peri = perimeters.list_for_org(db, org_id)
    tests_for_form = (
        load_tests(db, organization_id=org_id)
        if perimeter_id is None
        else [t for t in load_tests(db, organization_id=org_id) if t.perimeter_id == perimeter_id]
    )
    return dict(
        models=models,
        testable_models=[
            m for m in models if (m.model_name or "").lower() in TESTABLE_PROVIDERS
        ],
        judgeable_models=judges,
        judge_kappa=judge_kappa,
        kappa_threshold=0.6,
        tests=tests_for_form,
        all_perimeters=all_peri,
    )


def _parse_run_selection(
    db: Session,
    org_id: int,
    perimeter_id: int,
    tested_models: list[str],
    judge_models: list[str],
    repeats: int,
    test_ids: list[int],
) -> dict:
    """Valide la sélection (modèles, juges, tests) restreinte au périmètre."""
    if not tested_models or not judge_models:
        raise HTTPException(
            status_code=400,
            detail="Sélectionne au moins une IA évaluée et un notateur.",
        )
    peri = perimeters.get_by_id(db, org_id, perimeter_id)
    if peri is None:
        raise HTTPException(status_code=400, detail="Périmètre invalide.")
    peri_tests = [
        t for t in load_tests(db, organization_id=org_id) if t.perimeter_id == perimeter_id
    ]
    all_active_ids = [t.test_id for t in peri_tests]
    if not all_active_ids:
        raise HTTPException(
            status_code=400,
            detail="Aucune question active et prête dans ce périmètre.",
        )
    if not test_ids:
        raise HTTPException(status_code=400, detail="Sélectionne au moins une question.")
    # Vérif : les test_ids demandés appartiennent au périmètre.
    invalid = set(test_ids) - set(all_active_ids)
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"Questions hors du périmètre {peri.name!r} : {sorted(invalid)}",
        )
    selected: Optional[list[int]] = None if set(test_ids) >= set(all_active_ids) else test_ids
    judges = [{"model": v, "repeats": max(1, repeats)} for v in judge_models]
    return dict(tested_models=tested_models, judges=judges, test_ids=selected)


@app.get("/o/{org_slug}/launch", response_class=HTMLResponse)
def launch_form(
    request: Request,
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
    perimeter_id: int = 0,
):
    org, role = ctx
    peri = perimeters.get_by_id(db, org.id, perimeter_id) if perimeter_id > 0 else None
    fctx = _run_form_context(db, org.id, perimeter_id=peri.id if peri else None)
    b = budget.get_budget(db, org.id)
    spent = budget.current_month_spent(db, org.id)
    return render(request, "launch.html", active="launch", org=org, role=role,
                  active_tests_count=len(fctx["tests"]),
                  selected_perimeter=peri,
                  budget=b, month_spent=spent, **fctx)


@app.post("/o/{org_slug}/launch")
def launch_submit(
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
    perimeter_id: int = Form(...),
    tested_models: list[str] = Form(default=[]),
    judge_models: list[str] = Form(default=[]),
    repeats: int = Form(1),
    note: str = Form(""),
    test_ids: list[int] = Form(default=[]),
):
    org, _ = ctx
    params = _parse_run_selection(db, org.id, perimeter_id, tested_models, judge_models, repeats, test_ids)

    tests_for_estimate = load_tests(
        db, test_ids=params["test_ids"], active_only=True, ready_only=True,
        organization_id=org.id,
    )
    estimate = pricing.estimate_scan_cost(
        db, org_id=org.id, tests=tests_for_estimate,
        tested_models=params["tested_models"], judges=params["judges"],
    )
    check = budget.check_budget(db, org_id=org.id, estimate_eur=estimate["total_eur"])
    if not check.ok:
        raise HTTPException(status_code=402, detail=check.reason)

    job = manager.submit(dict(
        **params, note=note or None,
        organization_id=org.id, perimeter_id=perimeter_id,
    ))
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
    creds = credentials.list_for_org(db, org.id)
    creds_by_model = {c.model_id: c for c in creds}
    return render(request, "models.html", active="models", org=org, role=role,
                  models=models, refs=refs, creds_by_model=creds_by_model)


# ---- BYOK : configuration par org (org_admin+) --------------------
@app.get("/o/{org_slug}/credentials/{model_id}/edit", response_class=HTMLResponse)
def credential_edit_form(
    model_id: int,
    request: Request,
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
):
    org, role = ctx
    model = services.get_model(db, model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="Modèle introuvable.")
    cred = credentials.get_for_model(db, org.id, model_id)
    return render(
        request, "credential_form.html", active="models", org=org, role=role,
        model=model, cred=cred,
    )


@app.post("/o/{org_slug}/credentials/{model_id}/edit")
def credential_edit_submit(
    model_id: int,
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
    base_url: str = Form(""),
    api_key: str = Form(""),
    clear_api_key: bool = Form(False),
    extra_headers: str = Form(""),
    is_active: bool = Form(False),
):
    org, _ = ctx
    model = services.get_model(db, model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="Modèle introuvable.")
    credentials.upsert(
        db,
        org_id=org.id,
        model_id=model_id,
        base_url=base_url.strip() or None,
        api_key=api_key.strip() or None,
        clear_api_key=clear_api_key,
        extra_headers=_parse_headers(extra_headers),
        is_active=is_active,
    )
    audit.record(
        db,
        user_id=user.id,
        org_id=org.id,
        action="upsert",
        entity_type="org_credential",
        entity_id=model_id,
        meta={"model_version": model.model_version, "clear": clear_api_key},
    )
    return RedirectResponse(f"/o/{org.slug}/models", status_code=303)


@app.post("/o/{org_slug}/credentials/{model_id}/delete")
def credential_delete(
    model_id: int,
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
):
    org, _ = ctx
    credentials.delete(db, org.id, model_id)
    audit.record(
        db,
        user_id=user.id,
        org_id=org.id,
        action="delete",
        entity_type="org_credential",
        entity_id=model_id,
    )
    return RedirectResponse(f"/o/{org.slug}/models", status_code=303)


@app.get("/o/{org_slug}/models/new", response_class=HTMLResponse)
def model_new_form(
    request: Request,
    ctx=Depends(require_org),
    _pa: CurrentUser = Depends(require_platform_admin),
):
    org, role = ctx
    return render(request, "model_form.html", active="models", org=org, role=role,
                  model=None, providers=PROVIDER_CHOICES)


def _parse_search_config(
    engine: str, max_results: str, context_size: str, allowed_domains: str
) -> Optional[dict]:
    """Assemble models.search_config depuis les champs du formulaire (ADR-080 §2.2).

    engine vide ou "off" sans autre champ → None (pas de recherche web).
    """
    engine = (engine or "").strip().lower()
    if engine not in {"", "off", "native", "exa", "firecrawl"}:
        raise HTTPException(status_code=400, detail=f"engine invalide : {engine!r}")
    config: dict = {}
    if engine and engine != "off":
        config["engine"] = engine
    if (max_results or "").strip():
        try:
            n = int(max_results)
            if not 1 <= n <= 20:
                raise ValueError
        except ValueError:
            raise HTTPException(status_code=400, detail="max_results doit être un entier entre 1 et 20")
        config["max_results"] = n
    context_size = (context_size or "").strip().lower()
    if context_size:
        if context_size not in {"low", "medium", "high"}:
            raise HTTPException(status_code=400, detail=f"search_context_size invalide : {context_size!r}")
        config["search_context_size"] = context_size
    domains = [d.strip() for d in (allowed_domains or "").split(",") if d.strip()]
    if domains:
        config["allowed_domains"] = domains
    if engine == "off":
        return {"engine": "off"}  # explicite : recherche coupée malgré d'autres champs
    return config or None


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
    search_engine: str = Form(""),
    search_max_results: str = Form(""),
    search_context_size: str = Form(""),
    search_allowed_domains: str = Form(""),
):
    org, _ = ctx
    services.create_model(
        db,
        model_name=model_name.strip(),
        model_version=model_version.strip(),
        base_url=base_url.strip(),
        api_key=api_key.strip(),
        extra_headers=_parse_headers(extra_headers),
        search_config=_parse_search_config(
            search_engine, search_max_results, search_context_size, search_allowed_domains
        ),
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
    search_engine: str = Form(""),
    search_max_results: str = Form(""),
    search_context_size: str = Form(""),
    search_allowed_domains: str = Form(""),
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
        search_config=_parse_search_config(
            search_engine, search_max_results, search_context_size, search_allowed_domains
        ),
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
def schedule_new_form(
    request: Request,
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
    perimeter_id: int = 0,
):
    org, role = ctx
    peri = perimeters.get_by_id(db, org.id, perimeter_id) if perimeter_id > 0 else None
    fctx = _run_form_context(db, org.id, perimeter_id=peri.id if peri else None)
    return render(request, "schedule_form.html", active="schedules", org=org, role=role,
                  weekdays=scheduler.WEEKDAYS_FR,
                  selected_perimeter=peri, **fctx)


@app.post("/o/{org_slug}/schedules/new")
def schedule_new_submit(
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
    perimeter_id: int = Form(...),
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
    params = _parse_run_selection(db, org.id, perimeter_id, tested_models, judge_models, repeats, test_ids)

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

    # Devis prévisionnel + check budget (soft-stop, ADR-078).
    tests_for_estimate = load_tests(
        db,
        test_ids=params["test_ids"],
        active_only=True, ready_only=True, organization_id=org.id,
    )
    estimate = pricing.estimate_scan_cost(
        db, org_id=org.id, tests=tests_for_estimate,
        tested_models=params["tested_models"], judges=params["judges"],
    )
    check = budget.check_budget(db, org_id=org.id, estimate_eur=estimate["total_eur"])
    if not check.ok:
        raise HTTPException(status_code=402, detail=check.reason)

    services.create_schedule(
        db,
        org.id,
        perimeter_id=perimeter_id,
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
            perimeter_id=sr.perimeter_id,
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


# =====================================================================
# PR#15 — Budget par org + admin pricing
# =====================================================================
from decimal import Decimal as _Decimal  # noqa: E402


@app.get("/o/{org_slug}/budget", response_class=HTMLResponse)
def org_budget_view(
    request: Request,
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
):
    org, role = ctx
    b = budget.get_budget(db, org.id)
    spent = budget.current_month_spent(db, org.id)
    pct = None
    if b is not None and b.monthly_cap_eur:
        pct = min(100, int((spent / _Decimal(str(b.monthly_cap_eur))) * 100))
    return render(
        request, "org_budget.html", active="settings", org=org, role=role,
        budget=b, month_spent=spent, pct=pct,
    )


@app.post("/o/{org_slug}/budget")
def org_budget_set(
    ctx=Depends(require_role("org_admin")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
    monthly_cap_eur: str = Form(...),
):
    org, _ = ctx
    try:
        cap = _Decimal(monthly_cap_eur.replace(",", "."))
        if cap < 0:
            raise ValueError("négatif")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Cap invalide : {e}")
    budget.set_cap(db, org_id=org.id, cap_eur=cap, updated_by=user.id)
    audit.record(
        db,
        user_id=user.id, org_id=org.id, action="set_cap",
        entity_type="budget", entity_id=org.id, meta={"cap_eur": str(cap)},
    )
    return RedirectResponse(f"/o/{org.slug}/budget", status_code=303)


@app.get("/admin/pricing", response_class=HTMLResponse)
def admin_pricing_view(
    request: Request,
    user: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    entries = pricing.list_pricing(db)
    return templates.TemplateResponse(
        request=request,
        name="admin_pricing.html",
        context={
            "active": "admin", "user": user, "url_prefix": "",
            "entries": entries,
        },
    )


@app.post("/admin/pricing/{model_id}")
def admin_pricing_set(
    model_id: int,
    user: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
    input_price: str = Form(...),
    output_price: str = Form(...),
):
    try:
        ip = _Decimal(input_price.replace(",", "."))
        op = _Decimal(output_price.replace(",", "."))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Prix invalide : {e}")
    pricing.set_pricing(
        db, model_id=model_id, input_eur_per_1m=ip, output_eur_per_1m=op,
    )
    audit.record(
        db,
        user_id=user.id, org_id=None, action="set_pricing",
        entity_type="model_pricing", entity_id=model_id,
        meta={"input": str(ip), "output": str(op)},
    )
    return RedirectResponse("/admin/pricing", status_code=303)


# =====================================================================
# EPIC-001 Phase 5 — Import catalogue + pricing OpenRouter (S5.1/S5.2)
# =====================================================================
from urllib.parse import urlencode  # noqa: E402

_OR_IMPORT_MESSAGES: dict[str, tuple[str, str]] = {
    "created": ("success", "Modèle créé au catalogue GEOeval (prix importé si disponible)."),
    "created_unpriced": ("success", "Modèle créé au catalogue GEOeval — prix indisponible côté OpenRouter, à saisir dans « Tarifs des IA »."),
    "exists": ("info", "Ce modèle existe déjà au catalogue GEOeval — rien à créer."),
    "price_updated": ("success", "Prix importé : nouvelle version de tarif créée (l'ancienne est clôturée)."),
    "price_same": ("info", "Prix identique à la version en vigueur — aucune nouvelle version créée."),
    "not_found": ("error", "Modèle introuvable au catalogue GEOeval — crée-le d'abord."),
}


def _or_import_redirect(msg: str, q: str) -> RedirectResponse:
    params = urlencode({"load": 1, "q": q, "msg": msg})
    return RedirectResponse(f"/admin/openrouter-import?{params}", status_code=303)


@app.get("/admin/openrouter-import", response_class=HTMLResponse)
def admin_openrouter_import_view(
    request: Request,
    user: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
    load: int = 0,
    q: str = "",
    msg: str = "",
):
    """Écran d'import du catalogue OpenRouter — fetch à la demande uniquement."""
    entries: list[openrouter_catalog.CatalogEntry] = []
    existing: dict[str, object] = {}
    error: Optional[str] = None
    total = 0
    if load:
        try:
            entries = openrouter_catalog.fetch_catalog()
        except openrouter_catalog.CatalogError as exc:
            error = str(exc)
        total = len(entries)
        query = q.strip().lower()
        if query:
            entries = [
                e for e in entries
                if query in e.id.lower() or query in e.name.lower()
            ]
        existing = openrouter_catalog.existing_openrouter_models(db)
    kind, text = _OR_IMPORT_MESSAGES.get(msg, ("", ""))
    return templates.TemplateResponse(
        request=request,
        name="admin_openrouter_import.html",
        context={
            "active": "admin", "user": user, "url_prefix": "",
            "loaded": bool(load), "entries": entries, "existing": existing,
            "total": total, "q": q, "error": error,
            "msg_kind": kind, "msg_text": text,
            "rate": openrouter_catalog.usd_eur_rate(),
        },
    )


@app.post("/admin/openrouter-import/create")
def admin_openrouter_import_create(
    user: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
    model_ref: str = Form(...),
    prompt_usd: str = Form(""),
    completion_usd: str = Form(""),
    q: str = Form(""),
):
    """Crée en 1 clic la ligne `models` (S5.1) + importe le prix (S5.2)."""
    model_ref = model_ref.strip()
    if not model_ref:
        raise HTTPException(status_code=400, detail="Identifiant OpenRouter manquant.")
    if openrouter_catalog.get_openrouter_model(db, model_ref) is not None:
        return _or_import_redirect("exists", q)  # idempotent
    model = services.create_model(
        db,
        model_name="openrouter",
        model_version=model_ref,
        base_url=None,          # défaut famille openrouter (llm_clients)
        api_key=None,           # clé plateforme via env
        extra_headers=None,
        search_config=None,     # à configurer ensuite par l'admin (ADR-080 §2.2)
    )
    priced = openrouter_catalog.import_pricing(
        db,
        model_id=model.model_id,
        prompt_usd_per_token=openrouter_catalog.parse_price(prompt_usd),
        completion_usd_per_token=openrouter_catalog.parse_price(completion_usd),
    )
    audit.record(
        db,
        user_id=user.id, org_id=None, action="import",
        entity_type="model", entity_id=model.model_id,
        meta={"source": "openrouter", "model_version": model_ref, "pricing_imported": priced},
    )
    return _or_import_redirect("created" if priced else "created_unpriced", q)


@app.post("/admin/openrouter-import/pricing")
def admin_openrouter_import_pricing(
    user: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
    model_ref: str = Form(...),
    prompt_usd: str = Form(""),
    completion_usd: str = Form(""),
    q: str = Form(""),
):
    """Importe le prix OpenRouter d'un modèle déjà présent (S5.2, versionné)."""
    model = openrouter_catalog.get_openrouter_model(db, model_ref.strip())
    if model is None:
        return _or_import_redirect("not_found", q)
    changed = openrouter_catalog.import_pricing(
        db,
        model_id=model.model_id,
        prompt_usd_per_token=openrouter_catalog.parse_price(prompt_usd),
        completion_usd_per_token=openrouter_catalog.parse_price(completion_usd),
    )
    if changed:
        audit.record(
            db,
            user_id=user.id, org_id=None, action="set_pricing",
            entity_type="model_pricing", entity_id=model.model_id,
            meta={"source": "openrouter", "model_version": model.model_version},
        )
    return _or_import_redirect("price_updated" if changed else "price_same", q)


# =====================================================================
# PR#17 — Gold set + métriques d'accord
# =====================================================================
from fastapi import UploadFile, File  # noqa: E402
from models import Model as _Model  # noqa: E402
from sqlalchemy import select as _sel2  # noqa: E402

KAPPA_WARNING_THRESHOLD = 0.6


@app.get("/admin/gold-annotations/import", response_class=HTMLResponse)
def gold_import_form(
    request: Request,
    user: CurrentUser = Depends(require_platform_admin),
):
    return templates.TemplateResponse(
        request=request,
        name="admin_gold_import.html",
        context={"active": "admin", "user": user, "url_prefix": "", "result": None},
    )


@app.post("/admin/gold-annotations/import", response_class=HTMLResponse)
async def gold_import_submit(
    request: Request,
    user: CurrentUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
    file: UploadFile = File(...),
):
    content = await file.read()
    result = gold.import_csv(db, content)
    audit.record(
        db, user_id=user.id, org_id=None,
        action="import_csv", entity_type="gold_annotations",
        entity_id=None,
        meta={"inserted": result["inserted"], "rejected": len(result["rejected"])},
    )
    return templates.TemplateResponse(
        request=request,
        name="admin_gold_import.html",
        context={"active": "admin", "user": user, "url_prefix": "", "result": result},
    )


def _judges_agreement_matrix(session):
    """Renvoie [{model, agreement}] pour tous les modèles marqués juge."""
    judges = session.execute(
        _sel2(_Model).where(_Model.is_judge.is_(True))
    ).scalars().all()
    out = []
    for j in judges:
        ag = agreement.compute_agreement_vs_gold(session, j.model_id)
        out.append(dict(
            model_id=j.model_id,
            model_name=j.model_name,
            model_version=j.model_version,
            is_sovereign=j.is_sovereign,
            agreement=ag,
        ))
    return out


@app.get("/methodology/judges", response_class=HTMLResponse)
def methodology_judges(
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    # Accessible à tout user connecté (info méthodologique).
    matrix = _judges_agreement_matrix(db)
    return templates.TemplateResponse(
        request=request,
        name="methodology_judges.html",
        context={
            "active": "methodology", "user": user, "url_prefix": "",
            "matrix": matrix, "threshold": KAPPA_WARNING_THRESHOLD,
        },
    )
