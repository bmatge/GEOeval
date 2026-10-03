"""
Exécution des runs programmés (table scheduled_runs).

Le processus worker (geoeval.worker.main) appelle `tick_if_leader()` toutes les
POLL_SECONDS : les planifications échues sont mises en file dans `jobs`, sous
verrou consultatif PostgreSQL pour qu'un seul worker le fasse à la fois. Tout
l'état vit en base. Les heures saisies dans l'UI sont en Europe/Paris ;
next_run_at est stocké en UTC.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from geoeval.db.models import ScheduledRun

logger = logging.getLogger("geoeval.worker.scheduler")

TZ_PARIS = ZoneInfo("Europe/Paris")
POLL_SECONDS = 30

SCHEDULE_KINDS = ("once", "daily", "weekly", "every_n_hours")
WEEKDAYS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def compute_next_run(kind: str, config: dict[str, Any],
                     after: Optional[datetime] = None) -> Optional[datetime]:
    """Prochaine échéance (UTC, tz-aware) strictement après `after` (défaut : maintenant).

    Configs : once={"at": "YYYY-MM-DDTHH:MM"} (heure de Paris) ;
    daily={"time": "HH:MM"} ; weekly={"weekday": 0-6, "time": "HH:MM"} ;
    every_n_hours={"hours": N}.
    Retourne None pour un one-shot déjà passé.
    """
    now = after or datetime.now(timezone.utc)

    if kind == "once":
        local = datetime.fromisoformat(config["at"]).replace(tzinfo=TZ_PARIS)
        at = local.astimezone(timezone.utc)
        return at if at > now else None

    if kind == "daily":
        hh, mm = map(int, config["time"].split(":"))
        local_now = now.astimezone(TZ_PARIS)
        candidate = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= local_now:
            candidate += timedelta(days=1)
        return candidate.astimezone(timezone.utc)

    if kind == "weekly":
        hh, mm = map(int, config["time"].split(":"))
        target_wd = int(config["weekday"])  # 0 = lundi
        local_now = now.astimezone(TZ_PARIS)
        candidate = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        days_ahead = (target_wd - candidate.weekday()) % 7
        candidate += timedelta(days=days_ahead)
        if candidate <= local_now:
            candidate += timedelta(days=7)
        return candidate.astimezone(timezone.utc)

    if kind == "every_n_hours":
        return now + timedelta(hours=int(config["hours"]))

    raise ValueError(f"schedule_kind inconnu: {kind!r}")


def describe_schedule(kind: str, config: dict[str, Any]) -> str:
    """Libellé humain pour l'UI (heures de Paris)."""
    if kind == "once":
        return f"une fois, le {config['at'].replace('T', ' à ')}"
    if kind == "daily":
        return f"chaque jour à {config['time']}"
    if kind == "weekly":
        return f"chaque {WEEKDAYS_FR[int(config['weekday'])]} à {config['time']}"
    if kind == "every_n_hours":
        return f"toutes les {config['hours']} h"
    return kind

# Clé du verrou consultatif PostgreSQL (portée transaction) : un seul tick à la
# fois, quel que soit le nombre de workers. 0x47454F45 = "GEOE".
SCHEDULER_LOCK_KEY = 0x47454F45


def tick_detailed(session: Session) -> tuple[int, set[int]]:
    """Met en file les planifications échues et recalcule leur prochaine échéance.

    E3 : le budget consolidé est revérifié au moment de l'échéance. Si l'exécution
    dépasserait un plafond de la chaîne, elle est SAUTÉE (pas de mise en file), le
    motif est enregistré sur la planification et dans le journal d'audit, et la
    prochaine échéance est recalculée normalement (un one-shot sauté est clos).
    E5 : même traitement si la politique de routage refuse la sélection ou si le
    contrat le plus proche d'un modèle est expiré ou épuisé (audit `skip_routing`,
    `skip_contract`).
    Ne commite pas : l'appelant tient la transaction (et le verrou).
    Renvoie (nombre mis en file, entités dont une échéance a été sautée).
    """
    from geoeval.db.models import AuditLog
    from geoeval.web import launching
    from geoeval.worker import jobs

    now = datetime.now(timezone.utc)
    due = session.execute(
        select(ScheduledRun).where(
            ScheduledRun.enabled.is_(True),
            ScheduledRun.next_run_at.is_not(None),
            ScheduledRun.next_run_at <= now,
        )
    ).scalars().all()

    queued = 0
    skipped_orgs: set[int] = set()
    for sr in due:
        params = dict(
            tested_models=list(sr.tested_models),
            judges=list(sr.judges),
            test_ids=list(sr.test_ids) if sr.test_ids else None,
        )
        skip_reason: Optional[str] = None
        skip_kind = "budget"
        try:
            launching.estimate_and_check_budget(
                session, sr.organization_id, params, perimeter_id=sr.perimeter_id,
            )
        except launching.LaunchError as exc:
            # E3 : budget ; E5 : politique de routage ou contrat inutilisable.
            if exc.kind in launching.SKIPPABLE_KINDS:
                skip_reason, skip_kind = exc.detail, exc.kind
            else:
                raise
        except Exception:  # noqa: BLE001 — un devis en échec ne bloque pas le suivi longitudinal
            logger.exception("devis impossible pour la planification %s : exécution maintenue", sr.schedule_id)

        if skip_reason is not None:
            logger.warning("planification %s (%s) sautée : %s", sr.schedule_id, sr.name, skip_reason)
            sr.last_skipped_at = now
            sr.last_skip_reason = skip_reason
            session.add(AuditLog(
                user_id=None, org_id=sr.organization_id, action=f"skip_{skip_kind}", entity_type="scheduled_run",
                entity_id=sr.schedule_id, meta_json={"name": sr.name, "reason": skip_reason},
            ))
            if skip_kind == "budget":
                skipped_orgs.add(sr.organization_id)
        else:
            job = jobs.submit(
                session,
                dict(**params, organization_id=sr.organization_id, perimeter_id=sr.perimeter_id,
                     note=sr.note or f"planifié : {sr.name}"),
                commit=False,
            )
            logger.info("planification %s (%s) -> job %s", sr.schedule_id, sr.name, job.id)
            sr.last_run_at = now
            sr.last_job_id = job.id
            sr.last_skip_reason = None
            queued += 1
        if sr.schedule_kind == "once":
            sr.enabled = False
            sr.next_run_at = None
        else:
            sr.next_run_at = compute_next_run(sr.schedule_kind, sr.schedule_config, after=now)
    # E8 : campagnes échues, une exécution par participant (sautée et tracée si refusée).
    from geoeval.web import campaigns

    c_queued, c_budget_orgs = campaigns.tick(session, now)
    return queued + c_queued, skipped_orgs | c_budget_orgs


def tick(session: Session) -> int:
    """Compatibilité : nombre d'exécutions mises en file."""
    return tick_detailed(session)[0]


def tick_if_leader(session: Session) -> Optional[int]:
    """Exécute `tick()` sous verrou consultatif transactionnel. Renvoie le nombre
    de planifications traitées, ou None si un autre worker tenait le verrou."""
    got = session.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": SCHEDULER_LOCK_KEY}).scalar_one()
    if not got:
        session.rollback()
        return None
    try:
        n, skipped_orgs = tick_detailed(session)
        session.commit()  # libère le verrou
    except Exception:
        session.rollback()
        raise
    if skipped_orgs:
        from geoeval.web import budget_alerts

        for org_id in skipped_orgs:
            budget_alerts.evaluate_safely(org_id)
    # E7 : contrats expirants / expirés (au plus une fois par heure, idempotent).
    from geoeval.web import detectors

    detectors.check_contracts_throttled(session)
    return n
