"""
Jobs d'évaluation persistés en base (ADR-088, lot 1.2).

La file est la table `jobs`. Un ou plusieurs processus worker
(`python -m geoeval.worker.main`) réclament les jobs par
`SELECT … FOR UPDATE SKIP LOCKED`, exécutent RUN puis ÉVALUATION pour chaque
modèle testé, et écrivent progression, battement de cœur et logs en base.
L'UI et l'API lisent la base : plus rien ne vit en mémoire, un redémarrage
ne perd ni la file ni l'historique des jobs.

Arbitrages (ADR-088 §2.1, validés) :
- un job interrompu par l'arrêt du worker passe en `error` avec un motif
  explicite ; **jamais de relance automatique** (pas de doublon de runs,
  ADR-076, pas de dépense LLM sans décision humaine) ;
- les logs vont dans `job_logs` (une ligne par message) et sur stdout.
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.orm import Session

from geoeval.core.evaluate import evaluate_run
from geoeval.core.load import load_tests
from geoeval.core.run import execute_run
from geoeval.db.models import Job, JobLog
from geoeval.db.session import SessionLocal
from geoeval.observability import metrics
from geoeval.observability.context import job_id_var, org_id_var

logger = logging.getLogger("geoeval.worker.jobs")

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"

HEARTBEAT_SECONDS = 20          # battement de cœur pendant l'exécution
STALE_AFTER_SECONDS = 180       # running sans battement depuis > 3 min ⇒ interrompu
LOG_TAIL = 400                  # lignes renvoyées à l'UI (comme l'ancienne deque)

INTERRUPTED_MESSAGE = (
    "Interrompu : le worker s'est arrêté pendant l'exécution. Les évaluations "
    "déjà écrites sont conservées ; relancer si nécessaire."
)


def worker_identity() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def new_job_id() -> str:
    import uuid

    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------
# Accès (web + worker)
# ---------------------------------------------------------------------
def submit(session: Session, params: dict[str, Any], *, priority: int = 0, commit: bool = True) -> Job:
    """Met un job en file. `params` porte obligatoirement `organization_id`."""
    job = Job(
        id=new_job_id(),
        organization_id=int(params["organization_id"]),
        status=STATUS_QUEUED,
        priority=priority,
        params=params,
        run_ids=[],
    )
    session.add(job)
    if commit:
        session.commit()
    else:
        session.flush()
    logger.info("Job %s en file (org=%s, modèles=%s)", job.id, job.organization_id, params.get("tested_models"))
    return job


def get(session: Session, job_id: str) -> Optional[Job]:
    return session.get(Job, job_id)


def get_for_org(session: Session, org_id: int, job_id: str) -> Optional[Job]:
    """Job d'une organisation donnée, None sinon (isolation tenant)."""
    job = get(session, job_id)
    if job is None or job.organization_id != org_id:
        return None
    return job


def list_for_org(session: Session, org_id: int, *, limit: int = 50) -> list[Job]:
    stmt = (
        select(Job)
        .where(Job.organization_id == org_id)
        .order_by(Job.created_at.desc())
        .limit(limit)
    )
    return list(session.execute(stmt).scalars().all())


def tail_logs(session: Session, job_id: str, *, limit: int = LOG_TAIL) -> list[str]:
    rows = session.execute(
        select(JobLog.message).where(JobLog.job_id == job_id).order_by(JobLog.id.desc()).limit(limit)
    ).scalars().all()
    return list(reversed(rows))


def as_dict(job: Job, *, log: Optional[list[str]] = None) -> dict[str, Any]:
    """Représentation attendue par les gabarits et l'API `/api/jobs/{id}`."""
    pct = int(100 * job.current / job.total) if job.total else 0
    return dict(
        id=job.id,
        status=job.status,
        phase=job.phase,
        current=job.current,
        total=job.total,
        pct=pct,
        error=job.error,
        run_ids=list(job.run_ids or []),
        log=list(log or []),
        created_at=job.created_at.isoformat() if job.created_at else None,
        params=dict(job.params or {}),
        worker_id=job.worker_id,
        attempt=job.attempt,
    )


# ---------------------------------------------------------------------
# Côté worker
# ---------------------------------------------------------------------
def claim_next(session: Session, worker_id: str) -> Optional[Job]:
    """Réclame le prochain job en file (priorité puis ancienneté), sans se
    marcher dessus entre workers (FOR UPDATE SKIP LOCKED)."""
    job_id = session.execute(
        text(
            """
            UPDATE jobs
               SET status = 'running', claimed_at = now(), heartbeat_at = now(),
                   worker_id = :worker_id, attempt = attempt + 1
             WHERE id = (
                   SELECT id FROM jobs
                    WHERE status = 'queued'
                    ORDER BY priority DESC, created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
             )
         RETURNING id
            """
        ),
        {"worker_id": worker_id},
    ).scalar_one_or_none()
    session.commit()
    return session.get(Job, job_id) if job_id else None


def recover_stale(session: Session, *, stale_after: int = STALE_AFTER_SECONDS) -> int:
    """Passe en `error` les jobs `running` sans battement de cœur récent
    (worker tué, conteneur redémarré). Pas de relance automatique."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after)
    res = session.execute(
        update(Job)
        .where(
            Job.status == STATUS_RUNNING,
            or_(Job.heartbeat_at.is_(None), Job.heartbeat_at < cutoff),
        )
        .values(status=STATUS_ERROR, error=INTERRUPTED_MESSAGE, phase="interrompu", finished_at=func.now())
    )
    session.commit()
    n = res.rowcount or 0
    if n:
        logger.warning("%d job(s) interrompu(s) marqué(s) en erreur (battement de cœur périmé)", n)
    return n


def _set(job_id: str, **fields: Any) -> None:
    with SessionLocal() as session:
        session.execute(update(Job).where(Job.id == job_id).values(heartbeat_at=func.now(), **fields))
        session.commit()


def _heartbeat_loop(job_id: str, stop: threading.Event) -> None:
    while not stop.wait(HEARTBEAT_SECONDS):
        try:
            _set(job_id)
            metrics.touch_worker_alive()
        except Exception:  # noqa: BLE001
            logger.exception("battement de cœur du job %s en échec", job_id)


class _DbLogHandler(logging.Handler):
    """Copie en base (job_logs) les logs émis par le thread qui exécute le job."""

    def __init__(self, job_id: str, thread_id: int) -> None:
        super().__init__()
        self.job_id = job_id
        self.thread_id = thread_id

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread != self.thread_id:
            return
        try:
            with SessionLocal() as session:
                session.add(JobLog(job_id=self.job_id, level=record.levelname, message=self.format(record)))
                session.commit()
        except Exception:  # noqa: BLE001 — un log qui échoue ne doit pas tuer le job
            pass


class JobInterrupted(RuntimeError):
    """Arrêt du worker demandé entre deux modèles : le job s'arrête proprement."""


def execute(job_id: str, *, stop_event: Optional[threading.Event] = None) -> None:
    """Exécute un job déjà réclamé (status=running) : RUN puis ÉVALUATION par
    modèle testé. Progression, logs et battement de cœur écrits en base."""
    handler = _DbLogHandler(job_id, threading.get_ident())
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    root = logging.getLogger()
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    root.addHandler(handler)

    hb_stop = threading.Event()
    threading.Thread(target=_heartbeat_loop, args=(job_id, hb_stop), daemon=True, name=f"hb-{job_id}").start()

    ctx_job = job_id_var.set(job_id)
    ctx_org = None
    alert_org: Optional[int] = None
    started = time.monotonic()
    outcome = "error"
    run_ids: list[int] = []
    try:
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise ValueError(f"job {job_id} introuvable")
            params = dict(job.params)

        tested_models: list[str] = params["tested_models"]
        judges: list[dict[str, Any]] = params["judges"]
        note: Optional[str] = params.get("note") or None
        test_ids: Optional[list[int]] = params.get("test_ids") or None
        organization_id = int(params["organization_id"])
        alert_org = organization_id
        ctx_org = org_id_var.set(organization_id)
        perimeter_id = params.get("perimeter_id")
        if perimeter_id is not None:
            perimeter_id = int(perimeter_id)

        logger.info("Job %s démarré (%d modèle(s) testé(s))", job_id, len(tested_models))

        for idx, tm in enumerate(tested_models):
            if stop_event is not None and stop_event.is_set():
                raise JobInterrupted(
                    f"Interrompu par l'arrêt du worker avant le modèle {tm} "
                    f"({idx}/{len(tested_models)} traité(s)). Les évaluations déjà écrites sont conservées."
                )

            # PHASE RUN
            with SessionLocal() as session:
                all_tests = load_tests(
                    session, test_ids=test_ids, active_only=True, ready_only=True,
                    organization_id=organization_id,
                )
                tests = (
                    [t for t in all_tests if t.perimeter_id == perimeter_id]
                    if perimeter_id is not None else all_tests
                )
                if not tests:
                    raise ValueError("Aucune question active et prête (dans le périmètre / la sélection).")

                def run_cb(cur: int, tot: int, detail: str, _tm=tm) -> None:
                    _set(job_id, phase=f"RUN {_tm} · {detail}", current=cur, total=tot)

                run_id = execute_run(
                    session, tested_model=tm, tests=tests, organization_id=organization_id,
                    perimeter_id=perimeter_id, run_meta={"note": note} if note else None,
                    progress_cb=run_cb,
                )
                session.commit()
                run_ids.append(run_id)
                _set(job_id, run_ids=list(run_ids))

            # PHASE ÉVALUATION
            with SessionLocal() as session:
                def eval_cb(cur: int, tot: int, detail: str, _tm=tm, _rid=run_id) -> None:
                    _set(job_id, phase=f"ÉVAL {_tm} (run {_rid}) · {detail}", current=cur, total=tot)

                evaluate_run(
                    session, run_id=run_id, judges=judges, organization_id=organization_id,
                    progress_cb=eval_cb,
                )
                session.commit()

        _set(job_id, status=STATUS_DONE, phase="terminé", finished_at=func.now())
        outcome = "done"
        logger.info("Job %s terminé (runs %s)", job_id, run_ids)
    except JobInterrupted as exc:
        outcome = "interrupted"
        logger.warning("Job %s : %s", job_id, exc)
        _set(job_id, status=STATUS_ERROR, error=str(exc), phase="interrompu", finished_at=func.now())
    except Exception as exc:  # noqa: BLE001
        logger.exception("Job %s en échec", job_id)
        _set(job_id, status=STATUS_ERROR, error=str(exc), finished_at=func.now())
    finally:
        hb_stop.set()
        root.removeHandler(handler)
        metrics.JOBS_EXECUTED.labels(outcome).inc()
        metrics.JOB_DURATION.observe(time.monotonic() - started)
        # E3 : la dépense a pu franchir un seuil (80 / 100 %) sur la chaîne d'entités.
        from geoeval.web import budget_alerts

        budget_alerts.evaluate_safely(alert_org)
        if ctx_org is not None:
            org_id_var.reset(ctx_org)
        job_id_var.reset(ctx_job)
