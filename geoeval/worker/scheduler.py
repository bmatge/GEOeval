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


def tick(session: Session) -> int:
    """Met en file les planifications échues et recalcule leur prochaine échéance.
    Ne commite pas : l'appelant tient la transaction (et le verrou)."""
    from geoeval.worker import jobs

    now = datetime.now(timezone.utc)
    due = session.execute(
        select(ScheduledRun).where(
            ScheduledRun.enabled.is_(True),
            ScheduledRun.next_run_at.is_not(None),
            ScheduledRun.next_run_at <= now,
        )
    ).scalars().all()

    for sr in due:
        job = jobs.submit(
            session,
            dict(
                organization_id=sr.organization_id,
                perimeter_id=sr.perimeter_id,
                tested_models=list(sr.tested_models),
                judges=list(sr.judges),
                note=sr.note or f"planifié : {sr.name}",
                test_ids=list(sr.test_ids) if sr.test_ids else None,
            ),
            commit=False,
        )
        logger.info("planification %s (%s) -> job %s", sr.schedule_id, sr.name, job.id)
        sr.last_run_at = now
        sr.last_job_id = job.id
        if sr.schedule_kind == "once":
            sr.enabled = False
            sr.next_run_at = None
        else:
            sr.next_run_at = compute_next_run(sr.schedule_kind, sr.schedule_config, after=now)
    return len(due)


def tick_if_leader(session: Session) -> Optional[int]:
    """Exécute `tick()` sous verrou consultatif transactionnel. Renvoie le nombre
    de planifications traitées, ou None si un autre worker tenait le verrou."""
    got = session.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": SCHEDULER_LOCK_KEY}).scalar_one()
    if not got:
        session.rollback()
        return None
    try:
        n = tick(session)
        session.commit()  # libère le verrou
        return n
    except Exception:
        session.rollback()
        raise
