"""Planifications de runs (once, daily, weekly, every_n_hours).

Router UI (lot 1.3a) : découpage mécanique de l'ancien app.py, sans changement
de comportement. Les règles métier vivent dans les services (geoeval.web.*).
"""
from __future__ import annotations


from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from geoeval.web import (
    scheduling,
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
        config = scheduling.build_config(
            schedule_kind, at=once_at or None, time=(daily_time if schedule_kind == "daily" else weekly_time) or None,
            weekday=weekly_weekday, hours=every_hours,
        )
        scheduling.create_schedule(
            db, org.id, perimeter_id=perimeter_id, name=name, tested_models=tested_models,
            judge_models=judge_models, repeats=repeats, test_ids=test_ids, note=note or None,
            schedule_kind=schedule_kind, schedule_config=config,
            role=role, is_platform_admin=user.is_platform_admin,
        )
    except launching.LaunchError as exc:
        raise http_error(exc)
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
    try:
        scheduling.set_enabled(db, org.id, sr, not sr.enabled)
    except launching.LaunchError as exc:
        raise http_error(exc)
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
