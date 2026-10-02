"""Planifications de runs (once, daily, weekly, every_n_hours).

Router UI (lot 1.3a) : découpage mécanique de l'ancien app.py, sans changement
de comportement. Les règles métier vivent dans les services (geoeval.web.*).
"""
from __future__ import annotations


from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from geoeval.web import (
    launching,
    perimeters,
    services,
)
from geoeval.web.auth import CurrentUser
from geoeval.web.deps import (
    get_db,
    require_org,
    require_role,
    require_user,
)
from geoeval.web.rendering import render
from geoeval.worker import scheduler

from geoeval.web.ui.launch import http_error, run_form_context

router = APIRouter()


@router.get("/o/{org_slug}/schedules", response_class=HTMLResponse)
def schedules_list(request: Request, ctx=Depends(require_org), db: Session = Depends(get_db)):
    org, role = ctx
    schedules = services.list_schedules(db, org.id)
    return render(
        request, "schedules.html", active="schedules", org=org, role=role,
        schedules=schedules, describe=scheduler.describe_schedule, tz=scheduler.TZ_PARIS,
    )


@router.get("/o/{org_slug}/schedules/new", response_class=HTMLResponse)
def schedule_new_form(
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
    return render(request, "schedule_form.html", active="schedules", org=org, role=role,
                  weekdays=scheduler.WEEKDAYS_FR,
                  selected_perimeter=peri, **fctx)


@router.post("/o/{org_slug}/schedules/new")
def schedule_new_submit(
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
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
    org, role = ctx
    try:
        params = launching.validate_selection(
            db, org.id, perimeter_id=perimeter_id, tested_models=tested_models,
            judge_models=judge_models, repeats=repeats, test_ids=test_ids,
            role=role, is_platform_admin=user.is_platform_admin,
        )
    except launching.LaunchError as exc:
        raise http_error(exc)

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

    # Devis prévisionnel + plafond budgétaire (règle portée par le service).
    try:
        launching.estimate_and_check_budget(db, org.id, params)
    except launching.LaunchError as exc:
        raise http_error(exc)

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


@router.post("/o/{org_slug}/schedules/{schedule_id}/toggle")
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


@router.post("/o/{org_slug}/schedules/{schedule_id}/run-now")
def schedule_run_now(
    schedule_id: int,
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
):
    org, _ = ctx
    sr = services.get_schedule(db, org.id, schedule_id)
    if sr is None:
        raise HTTPException(status_code=404, detail="Planification introuvable.")
    try:
        job = launching.run_schedule_now(db, org.id, sr)
    except launching.LaunchError as exc:
        raise http_error(exc)
    return RedirectResponse(f"/o/{org.slug}/jobs/{job.id}", status_code=303)


@router.post("/o/{org_slug}/schedules/{schedule_id}/delete")
def schedule_delete(
    schedule_id: int,
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
):
    org, _ = ctx
    services.delete_schedule(db, org.id, schedule_id)
    return RedirectResponse(f"/o/{org.slug}/schedules", status_code=303)
