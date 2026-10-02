"""
Règles des planifications (ADR-088 §2.3 — règles dans les services, lot 1.3c).

Construction et validation de la configuration d'une planification, création
(validation de la sélection, prochaine échéance, devis et plafond budgétaire),
activation / désactivation, suppression. Partagé par l'UI et l'API v1. Les
refus sont des `LaunchError` (kind="validation" ou "budget"), traduits par la
couche de présentation.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from geoeval.db.models import ScheduledRun
from geoeval.web import launching, services
from geoeval.web.launching import LaunchError
from geoeval.worker import scheduler

KINDS = scheduler.SCHEDULE_KINDS  # once, daily, weekly, every_n_hours


def build_config(
    kind: str,
    *,
    at: Optional[str] = None,
    time: Optional[str] = None,
    weekday: Optional[int] = None,
    hours: Optional[int] = None,
) -> dict[str, Any]:
    """Configuration normalisée d'une planification selon son type."""
    if kind == "once":
        if not at:
            raise LaunchError("validation", "Indique la date et l'heure d'exécution.")
        return {"at": at}
    if kind == "daily":
        if not time:
            raise LaunchError("validation", "Indique l'heure quotidienne.")
        return {"time": time}
    if kind == "weekly":
        if not time:
            raise LaunchError("validation", "Indique le jour et l'heure hebdomadaires.")
        wd = int(weekday or 0)
        if not 0 <= wd <= 6:
            raise LaunchError("validation", "Le jour de la semaine doit être compris entre 0 (lundi) et 6 (dimanche).")
        return {"weekday": wd, "time": time}
    if kind == "every_n_hours":
        if hours is None or int(hours) < 1:
            raise LaunchError("validation", "L'intervalle doit être d'au moins 1 heure.")
        return {"hours": int(hours)}
    raise LaunchError("validation", f"Type de planification inconnu : {kind!r}.")


def next_run_or_raise(kind: str, config: dict[str, Any]):
    try:
        next_run = scheduler.compute_next_run(kind, config)
    except (ValueError, KeyError) as exc:
        raise LaunchError("validation", f"Configuration de planification invalide : {exc}")
    if next_run is None:
        raise LaunchError("validation", "La date d'exécution est déjà passée.")
    return next_run


def create_schedule(
    session: Session,
    org_id: int,
    *,
    perimeter_id: int,
    name: str,
    tested_models: list[str],
    judge_models: list[str],
    repeats: int,
    test_ids: list[int],
    note: Optional[str],
    schedule_kind: str,
    schedule_config: dict[str, Any],
    role: Optional[str],
    is_platform_admin: bool,
) -> ScheduledRun:
    """Toutes les règles d'un lancement (liste blanche, périmètre, devis,
    budget) + la validité de l'échéance, puis enregistrement."""
    name = (name or "").strip()
    if not name:
        raise LaunchError("validation", "Le nom de la planification est obligatoire.")
    params = launching.validate_selection(
        session, org_id, perimeter_id=perimeter_id, tested_models=tested_models,
        judge_models=judge_models, repeats=repeats, test_ids=test_ids,
        role=role, is_platform_admin=is_platform_admin,
    )
    next_run = next_run_or_raise(schedule_kind, schedule_config)
    launching.estimate_and_check_budget(session, org_id, params)
    return services.create_schedule(
        session, org_id, perimeter_id=perimeter_id, name=name,
        tested_models=params["tested_models"], judges=params["judges"], test_ids=params["test_ids"],
        note=note or None, schedule_kind=schedule_kind, schedule_config=schedule_config, next_run_at=next_run,
    )


def set_enabled(session: Session, org_id: int, sr: ScheduledRun, enabled: bool) -> ScheduledRun:
    """Active (en recalculant l'échéance) ou désactive une planification."""
    if not enabled:
        services.set_schedule_enabled(session, org_id, sr.schedule_id, False)
    else:
        next_run = scheduler.compute_next_run(sr.schedule_kind, sr.schedule_config)
        if next_run is None:
            raise LaunchError(
                "validation",
                "Impossible de réactiver : la date one-shot est passée. Crée une nouvelle planification.",
            )
        services.set_schedule_enabled(session, org_id, sr.schedule_id, True, next_run_at=next_run)
    session.refresh(sr)
    return sr


def rename(session: Session, sr: ScheduledRun, name: str) -> ScheduledRun:
    name = (name or "").strip()
    if not name:
        raise LaunchError("validation", "Le nom de la planification est obligatoire.")
    sr.name = name
    session.commit()
    return sr


def delete(session: Session, org_id: int, sr: ScheduledRun) -> None:
    services.delete_schedule(session, org_id, sr.schedule_id)
