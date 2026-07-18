"""
Exécution des runs programmés (table scheduled_runs).

Un thread unique relit la table toutes les 30 s et soumet au JobManager les
planifications arrivées à échéance. Tout l'état vit en base : les
planifications survivent aux redéploiements (contrairement aux jobs, en
mémoire). Les heures saisies dans l'UI sont en Europe/Paris ; next_run_at est
stocké en UTC.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select

from db import SessionLocal
from models import ScheduledRun

logger = logging.getLogger("webapp.scheduler")

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


def _tick() -> None:
    # Import tardif : évite le cycle scheduler -> jobs -> (rien) au chargement.
    from webapp.jobs import manager

    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        due = session.execute(
            select(ScheduledRun).where(
                ScheduledRun.enabled.is_(True),
                ScheduledRun.next_run_at.is_not(None),
                ScheduledRun.next_run_at <= now,
            )
        ).scalars().all()

        for sr in due:
            job = manager.submit(
                dict(
                    organization_id=sr.organization_id,
                    perimeter_id=sr.perimeter_id,
                    tested_models=list(sr.tested_models),
                    judges=list(sr.judges),
                    note=sr.note or f"planifié : {sr.name}",
                    test_ids=list(sr.test_ids) if sr.test_ids else None,
                )
            )
            logger.info("planification %s (%s) -> job %s", sr.schedule_id, sr.name, job.id)
            sr.last_run_at = now
            sr.last_job_id = job.id
            if sr.schedule_kind == "once":
                sr.enabled = False
                sr.next_run_at = None
            else:
                sr.next_run_at = compute_next_run(sr.schedule_kind, sr.schedule_config, after=now)
        session.commit()


def _loop() -> None:
    while True:
        try:
            _tick()
        except Exception:  # noqa: BLE001 — le scheduler ne doit jamais mourir
            logger.exception("tick scheduler en échec")
        threading.Event().wait(POLL_SECONDS)


_started = False
_lock = threading.Lock()


def start() -> None:
    """Démarre le thread scheduler (idempotent, appelé au chargement de l'app)."""
    global _started
    with _lock:
        if _started:
            return
        threading.Thread(target=_loop, daemon=True, name="scheduler").start()
        _started = True
        logger.info("scheduler démarré (poll %ss)", POLL_SECONDS)
