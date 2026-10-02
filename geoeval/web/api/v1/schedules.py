"""Planifications — lecture (membres) et exécution immédiate (editor+)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from geoeval.web import audit, launching, scheduling, services
from geoeval.web.api.deps import Principal, require_role
from geoeval.web.api.schemas import JobOut, ScheduleIn, ScheduleOut, SchedulePatch
from geoeval.web.deps import get_db
from geoeval.worker import jobs as jobqueue
from geoeval.worker import scheduler

router = APIRouter(prefix="/orgs/{org_slug}/schedules", tags=["planifications"])


def _out(sr) -> ScheduleOut:
    out = ScheduleOut.model_validate(sr)
    out.description = scheduler.describe_schedule(sr.schedule_kind, sr.schedule_config)
    return out


@router.get("", response_model=list[ScheduleOut], summary="Planifications de l'organisation")
def list_schedules(principal: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    return [_out(sr) for sr in services.list_schedules(db, principal.org.id)]


@router.get("/{schedule_id}", response_model=ScheduleOut, summary="Une planification")
def get_schedule(schedule_id: int, principal: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    sr = services.get_schedule(db, principal.org.id, schedule_id)
    if sr is None:
        raise HTTPException(status_code=404, detail="Planification introuvable.")
    return _out(sr)


@router.post("/{schedule_id}/run-now", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED,
             summary="Exécuter maintenant (editor+), soumis au plafond budgétaire")
def run_now(schedule_id: int, principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    sr = services.get_schedule(db, principal.org.id, schedule_id)
    if sr is None:
        raise HTTPException(status_code=404, detail="Planification introuvable.")
    job = launching.run_schedule_now(db, principal.org.id, sr)
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action="run_now", entity_type="scheduled_run",
                 entity_id=sr.schedule_id, meta={"job_id": job.id, **principal.audit_meta()})
    return jobqueue.as_dict(job)


# ---- Écriture (lot 1.3c, editor+) -----------------------------------
@router.post("", response_model=ScheduleOut, status_code=status.HTTP_201_CREATED,
             summary="Créer une planification (editor+) — mêmes règles qu'un lancement + échéance valide")
def create_schedule(body: ScheduleIn, principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    config = scheduling.build_config(body.schedule_kind, at=body.at, time=body.time, weekday=body.weekday, hours=body.hours)
    sr = scheduling.create_schedule(
        db, principal.org.id, perimeter_id=body.perimeter_id, name=body.name, tested_models=body.tested_models,
        judge_models=body.judge_models, repeats=body.repeats, test_ids=body.test_ids, note=body.note,
        schedule_kind=body.schedule_kind, schedule_config=config,
        role=principal.role, is_platform_admin=principal.is_platform_admin,
    )
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action="create", entity_type="scheduled_run",
                 entity_id=sr.schedule_id, meta={"name": sr.name, "kind": sr.schedule_kind, **principal.audit_meta()})
    return _out(sr)


@router.patch("/{schedule_id}", response_model=ScheduleOut, summary="Activer / désactiver ou renommer (editor+)")
def update_schedule(schedule_id: int, body: SchedulePatch, principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    sr = services.get_schedule(db, principal.org.id, schedule_id)
    if sr is None:
        raise HTTPException(status_code=404, detail="Planification introuvable.")
    fields = body.model_dump(exclude_unset=True)
    if "name" in fields:
        scheduling.rename(db, sr, fields["name"])
    if "enabled" in fields and fields["enabled"] != sr.enabled:
        scheduling.set_enabled(db, principal.org.id, sr, fields["enabled"])
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action="update", entity_type="scheduled_run",
                 entity_id=sr.schedule_id, meta={"fields": sorted(fields), **principal.audit_meta()})
    db.refresh(sr)
    return _out(sr)


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Supprimer une planification (editor+)")
def delete_schedule(schedule_id: int, principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    sr = services.get_schedule(db, principal.org.id, schedule_id)
    if sr is None:
        raise HTTPException(status_code=404, detail="Planification introuvable.")
    scheduling.delete(db, principal.org.id, sr)
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action="delete", entity_type="scheduled_run",
                 entity_id=schedule_id, meta=principal.audit_meta())
    return Response(status_code=status.HTTP_204_NO_CONTENT)
