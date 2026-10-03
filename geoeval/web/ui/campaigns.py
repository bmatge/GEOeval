"""Campagnes (E8, ADR-089 §2.9) : protocole commun exécuté par des participants désignés.

Router UI mince : les règles vivent dans `geoeval.web.campaigns`. Lecture : membres
de l'entité propriétaire (toutes les lignes de résultats) ou d'une entité participante
(ses lignes seulement). Écriture : org_admin de l'entité propriétaire (la campagne
consomme le budget des participants).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from geoeval.db.models import Organization
from geoeval.web import audit, campaigns, hierarchy, launching, pools, scheduling
from geoeval.web.auth import CurrentUser
from geoeval.web.deps import get_db, require_org, require_role, require_user
from geoeval.web.rendering import render
from geoeval.worker import scheduler

router = APIRouter()


def _http(exc: campaigns.CampaignError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


def _form_ctx(db: Session, org, role, user: CurrentUser) -> dict:
    models = launching.allowed_models(db, org.id, role=role, is_platform_admin=user.is_platform_admin)
    return dict(
        visible_pools=pools.list_visible(db, org),
        testable_models=[m for m in models if (m.model_name or "").lower() in launching.TESTABLE_PROVIDERS],
        judge_models=[m for m in models if m.is_judge],
        candidates=[org] + hierarchy.descendants(db, org),
        weekdays=scheduler.WEEKDAYS_FR,
    )


def _config(schedule_kind: str, once_at: str, daily_time: str, weekly_weekday: int, weekly_time: str,
            every_hours: int) -> dict:
    try:
        return scheduling.build_config(
            schedule_kind, at=once_at or None, time=(daily_time if schedule_kind == "daily" else weekly_time) or None,
            weekday=weekly_weekday, hours=every_hours,
        )
    except launching.LaunchError as exc:
        raise HTTPException(status_code=400, detail=exc.detail)


@router.get("/o/{org_slug}/campaigns", response_class=HTMLResponse)
def campaigns_list(request: Request, ctx=Depends(require_org), db: Session = Depends(get_db)):
    org, role = ctx
    owned, joined = campaigns.list_for_org(db, org)
    return render(request, "campaigns.html", active="campaigns", org=org, role=role, owned=owned, joined=joined,
                  status_labels=campaigns.STATUS_LABELS,
                  n_participants={c.id: len(campaigns.participant_ids(db, c.id)) for c in owned})


@router.get("/o/{org_slug}/campaigns/new", response_class=HTMLResponse)
def campaign_new_form(request: Request, ctx=Depends(require_role("org_admin")), db: Session = Depends(get_db),
                      user: CurrentUser = Depends(require_user)):
    org, role = ctx
    return render(request, "campaign_form.html", active="campaigns", org=org, role=role, campaign=None,
                  selected_participants={org.id}, **_form_ctx(db, org, role, user))


@router.post("/o/{org_slug}/campaigns/new")
def campaign_create(
    ctx=Depends(require_role("org_admin")), db: Session = Depends(get_db), user: CurrentUser = Depends(require_user),
    name: str = Form(...), description: str = Form(""), source_pool_id: int = Form(0),
    tested_models: list[str] = Form(default=[]), judge_models: list[str] = Form(default=[]), repeats: int = Form(1),
    participant_ids: list[int] = Form(default=[]), schedule_kind: str = Form(...), once_at: str = Form(""),
    daily_time: str = Form(""), weekly_weekday: int = Form(0), weekly_time: str = Form(""), every_hours: int = Form(24),
):
    org, _ = ctx
    config = _config(schedule_kind, once_at, daily_time, weekly_weekday, weekly_time, every_hours)
    try:
        c = campaigns.create(
            db, org, name=name, description=description, source_pool_id=source_pool_id or None,
            tested_models=tested_models, judges=[{"model": j, "repeats": repeats} for j in judge_models],
            schedule_kind=schedule_kind, schedule_config=config, participant_ids=participant_ids, created_by=user.id,
        )
    except campaigns.CampaignError as e:
        raise _http(e)
    audit.record(db, user_id=user.id, org_id=org.id, action="create", entity_type="campaign", entity_id=c.id,
                 meta={"name": c.name})
    return RedirectResponse(f"/o/{org.slug}/campaigns/{c.id}", status_code=303)


@router.get("/o/{org_slug}/campaigns/{campaign_id}", response_class=HTMLResponse)
def campaign_detail(campaign_id: int, request: Request, ctx=Depends(require_org), db: Session = Depends(get_db)):
    org, role = ctx
    try:
        c, owned = campaigns.get_for_org(db, org, campaign_id)
    except campaigns.CampaignError as e:
        raise _http(e)
    pool = pools.get(db, c.source_pool_id) if c.source_pool_id else None
    tests = campaigns.snapshot_tests(db, c) if c.status == "draft" and c.source_pool_id else []
    return render(
        request, "campaign_detail.html", active="campaigns", org=org, role=role, campaign=c, owned=owned,
        can_edit=owned and role == "org_admin",
        owner=db.get(Organization, c.owner_org_id), pool=pool, draft_tests=tests,
        participants=campaigns.participants(db, c.id),
        results=campaigns.results(db, c, only_org_id=None if owned else org.id),
        status_labels=campaigns.STATUS_LABELS,
    )


@router.get("/o/{org_slug}/campaigns/{campaign_id}/edit", response_class=HTMLResponse)
def campaign_edit_form(campaign_id: int, request: Request, ctx=Depends(require_role("org_admin")),
                       db: Session = Depends(get_db), user: CurrentUser = Depends(require_user)):
    org, role = ctx
    try:
        c = campaigns.get_owned(db, org, campaign_id)
    except campaigns.CampaignError as e:
        raise _http(e)
    return render(request, "campaign_form.html", active="campaigns", org=org, role=role, campaign=c,
                  selected_participants=set(campaigns.participant_ids(db, c.id)),
                  rec_kind=c.schedule_kind, rec_config=c.schedule_config, **_form_ctx(db, org, role, user))


@router.post("/o/{org_slug}/campaigns/{campaign_id}/edit")
def campaign_edit_submit(
    campaign_id: int, ctx=Depends(require_role("org_admin")), db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
    name: str = Form(...), description: str = Form(""), source_pool_id: int = Form(0),
    tested_models: list[str] = Form(default=[]), judge_models: list[str] = Form(default=[]), repeats: int = Form(1),
    participant_ids: list[int] = Form(default=[]), schedule_kind: str = Form(""), once_at: str = Form(""),
    daily_time: str = Form(""), weekly_weekday: int = Form(0), weekly_time: str = Form(""), every_hours: int = Form(24),
):
    org, _ = ctx
    try:
        c = campaigns.get_owned(db, org, campaign_id)
        kwargs: dict = dict(name=name, description=description, set_description=True, participant_ids=participant_ids)
        if c.status == "draft":
            kwargs.update(source_pool_id=source_pool_id or None, set_source_pool=True, tested_models=tested_models,
                          judges=[{"model": j, "repeats": repeats} for j in judge_models],
                          schedule_kind=schedule_kind,
                          schedule_config=_config(schedule_kind, once_at, daily_time, weekly_weekday, weekly_time,
                                                  every_hours))
        campaigns.update(db, c, **kwargs)
    except campaigns.CampaignError as e:
        raise _http(e)
    audit.record(db, user_id=user.id, org_id=org.id, action="update", entity_type="campaign", entity_id=c.id,
                 meta={"name": c.name, "status": c.status})
    return RedirectResponse(f"/o/{org.slug}/campaigns/{c.id}", status_code=303)


def _action(org, db: Session, user: CurrentUser, campaign_id: int, action: str) -> Optional[str]:
    try:
        c = campaigns.get_owned(db, org, campaign_id)
        if action == "activate":
            campaigns.activate(db, c)
        elif action == "close":
            campaigns.close(db, c)
        elif action == "run-now":
            ex = campaigns.execute(db, c)
            audit.record(db, user_id=user.id, org_id=org.id, action="run_now", entity_type="campaign", entity_id=c.id,
                         meta={"queued": len(ex.queued), "skipped": len(ex.skipped)})
            return None
        elif action == "delete":
            campaigns.delete_draft(db, c)
            audit.record(db, user_id=user.id, org_id=org.id, action="delete", entity_type="campaign",
                         entity_id=campaign_id, meta={})
            return f"/o/{org.slug}/campaigns"
    except campaigns.CampaignError as e:
        raise _http(e)
    audit.record(db, user_id=user.id, org_id=org.id, action=action, entity_type="campaign", entity_id=campaign_id,
                 meta={})
    return None


@router.post("/o/{org_slug}/campaigns/{campaign_id}/{action}")
def campaign_action(campaign_id: int, action: str, ctx=Depends(require_role("org_admin")),
                    db: Session = Depends(get_db), user: CurrentUser = Depends(require_user)):
    org, _ = ctx
    if action not in ("activate", "close", "run-now", "delete"):
        raise HTTPException(status_code=404, detail="Action inconnue.")
    target = _action(org, db, user, campaign_id, action)
    return RedirectResponse(target or f"/o/{org.slug}/campaigns/{campaign_id}", status_code=303)
