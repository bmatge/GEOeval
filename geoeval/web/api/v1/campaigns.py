"""Campagnes (E8, ADR-089 §2.9) : protocole commun exécuté par des participants désignés.

Lecture : membres de l'entité propriétaire ou d'une entité participante (les résultats
d'un participant non propriétaire se limitent à ses lignes). Écriture : org_admin de
l'entité propriétaire.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from geoeval.db.models import Organization
from geoeval.web import audit, campaigns, scheduling
from geoeval.web.api.deps import Principal, require_role
from geoeval.web.api.schemas import (
    CampaignExecutionOut,
    CampaignIn,
    CampaignOut,
    CampaignParticipantOut,
    CampaignPatch,
    CampaignResultOut,
)
from geoeval.web.deps import get_db

router = APIRouter(prefix="/orgs/{org_slug}/campaigns", tags=["campagnes"])


def _http(exc: campaigns.CampaignError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


def _out(db: Session, c, org) -> CampaignOut:
    owner = db.get(Organization, c.owner_org_id)
    owned = owner.id == org.id
    parts = [CampaignParticipantOut(org_slug=o.slug, org_name=o.name, last_job_id=p.last_job_id,
                                    last_run_at=p.last_run_at, last_skip_reason=p.last_skip_reason)
             for p, o in campaigns.participants(db, c.id) if owned or o.id == org.id]
    return CampaignOut(
        id=c.id, owner_org_slug=owner.slug, owned=owned, name=c.name, description=c.description, status=c.status,
        source_pool_id=c.source_pool_id, tested_models=list(c.tested_models), judges=list(c.judges),
        protocol=c.protocol, schedule_kind=c.schedule_kind, schedule_config=c.schedule_config,
        next_run_at=c.next_run_at, last_run_at=c.last_run_at, activated_at=c.activated_at, closed_at=c.closed_at,
        participants=parts,
    )


def _visible(db: Session, principal: Principal, campaign_id: int):
    try:
        return campaigns.get_for_org(db, principal.org, campaign_id)
    except campaigns.CampaignError as exc:
        raise _http(exc)


def _owned(db: Session, principal: Principal, campaign_id: int):
    try:
        return campaigns.get_owned(db, principal.org, campaign_id)
    except campaigns.CampaignError as exc:
        raise _http(exc)


def _audit(db: Session, principal: Principal, action: str, campaign_id: int, **meta) -> None:
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action=action, entity_type="campaign",
                 entity_id=campaign_id, meta={**meta, **principal.audit_meta()})


@router.get("", response_model=list[CampaignOut], summary="Campagnes possédées et campagnes auxquelles l'entité participe")
def list_campaigns(principal: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    owned, joined = campaigns.list_for_org(db, principal.org)
    return [_out(db, c, principal.org) for c in owned] + [_out(db, c, principal.org) for c, _ in joined]


@router.post("", response_model=CampaignOut, status_code=status.HTTP_201_CREATED,
             summary="Créer une campagne en brouillon (org_admin)")
def create_campaign(body: CampaignIn, principal: Principal = Depends(require_role("org_admin")), db: Session = Depends(get_db)):
    config = scheduling.build_config(body.schedule_kind, at=body.at, time=body.time, weekday=body.weekday, hours=body.hours)
    try:
        c = campaigns.create(
            db, principal.org, name=body.name, description=body.description, source_pool_id=body.source_pool_id,
            tested_models=body.tested_models, judges=[{"model": j, "repeats": body.repeats} for j in body.judge_models],
            schedule_kind=body.schedule_kind, schedule_config=config, participant_ids=body.participant_ids,
            created_by=principal.user_id,
        )
    except campaigns.CampaignError as exc:
        raise _http(exc)
    _audit(db, principal, "create", c.id, name=c.name)
    return _out(db, c, principal.org)


@router.get("/{campaign_id}", response_model=CampaignOut, summary="Une campagne (propriétaire ou participant)")
def get_campaign(campaign_id: int, principal: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    c, _ = _visible(db, principal, campaign_id)
    return _out(db, c, principal.org)


@router.patch("/{campaign_id}", response_model=CampaignOut, summary="Modifier une campagne (org_admin) — protocole figé une fois active")
def update_campaign(campaign_id: int, body: CampaignPatch, principal: Principal = Depends(require_role("org_admin")),
                    db: Session = Depends(get_db)):
    c = _owned(db, principal, campaign_id)
    fields = body.model_dump(exclude_unset=True)
    kwargs: dict = {}
    if "name" in fields:
        kwargs["name"] = fields["name"]
    if "description" in fields:
        kwargs.update(description=fields["description"], set_description=True)
    if "source_pool_id" in fields:
        kwargs.update(source_pool_id=fields["source_pool_id"], set_source_pool=True)
    if "tested_models" in fields:
        kwargs["tested_models"] = fields["tested_models"] or []
    if "judge_models" in fields or "repeats" in fields:
        repeats = fields.get("repeats") or (c.judges[0]["repeats"] if c.judges else 1)
        versions = fields["judge_models"] if "judge_models" in fields else [j["model"] for j in c.judges]
        kwargs["judges"] = [{"model": v, "repeats": repeats} for v in versions or []]
    if "participant_ids" in fields:
        kwargs["participant_ids"] = fields["participant_ids"] or []
    try:
        campaigns.update(db, c, **kwargs)
    except campaigns.CampaignError as exc:
        raise _http(exc)
    _audit(db, principal, "update", c.id, fields=sorted(fields))
    return _out(db, c, principal.org)


@router.delete("/{campaign_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Supprimer un brouillon (org_admin)")
def delete_campaign(campaign_id: int, principal: Principal = Depends(require_role("org_admin")), db: Session = Depends(get_db)):
    c = _owned(db, principal, campaign_id)
    try:
        campaigns.delete_draft(db, c)
    except campaigns.CampaignError as exc:
        raise _http(exc)
    _audit(db, principal, "delete", campaign_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{campaign_id}/activate", response_model=CampaignOut, summary="Activer : fige le protocole (org_admin)")
def activate_campaign(campaign_id: int, principal: Principal = Depends(require_role("org_admin")), db: Session = Depends(get_db)):
    c = _owned(db, principal, campaign_id)
    try:
        campaigns.activate(db, c)
    except campaigns.CampaignError as exc:
        raise _http(exc)
    _audit(db, principal, "activate", c.id, n_tests=len(c.protocol["test_ids"]))
    return _out(db, c, principal.org)


@router.post("/{campaign_id}/run-now", response_model=CampaignExecutionOut,
             summary="Exécuter maintenant pour chaque participant (org_admin) — les participants bloqués sont sautés")
def run_campaign_now(campaign_id: int, principal: Principal = Depends(require_role("org_admin")), db: Session = Depends(get_db)):
    c = _owned(db, principal, campaign_id)
    try:
        ex = campaigns.execute(db, c)
    except campaigns.CampaignError as exc:
        raise _http(exc)
    _audit(db, principal, "run_now", c.id, queued=len(ex.queued), skipped=len(ex.skipped))
    return CampaignExecutionOut(
        queued=[{"organization_id": o, "job_id": j} for o, j in ex.queued],
        skipped=[{"organization_id": o, "kind": k, "reason": r} for o, k, r in ex.skipped],
    )


@router.post("/{campaign_id}/close", response_model=CampaignOut, summary="Clore la campagne (org_admin) — les runs restent")
def close_campaign(campaign_id: int, principal: Principal = Depends(require_role("org_admin")), db: Session = Depends(get_db)):
    c = _owned(db, principal, campaign_id)
    try:
        campaigns.close(db, c)
    except campaigns.CampaignError as exc:
        raise _http(exc)
    _audit(db, principal, "close", c.id)
    return _out(db, c, principal.org)


@router.get("/{campaign_id}/results", response_model=list[CampaignResultOut],
            summary="Comparaison participants × IA évaluée (un participant ne voit que ses lignes)")
def campaign_results(campaign_id: int, principal: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    c, owned = _visible(db, principal, campaign_id)
    rows = campaigns.results(db, c, only_org_id=None if owned else principal.org.id)
    return [CampaignResultOut(org_slug=r.org.slug, model_version=r.model_version, n_runs=r.n_runs,
                              last_run_id=r.last_run_id, last_at=r.last_at, last_response=r.last_response,
                              last_citation=r.last_citation, delta_response=r.delta_response,
                              delta_citation=r.delta_citation) for r in rows]
