"""
Exécution des runs en tâche de fond + suivi de progression.

Un worker unique (thread) traite une file de jobs : les runs sont donc
sérialisés (un à la fois), ce qui évite de saturer les quotas des API LLM et
simplifie la capture des logs. Chaque job capture les logs GEOeval émis pendant
son exécution pour affichage live dans l'UI.
"""
from __future__ import annotations

import logging
import queue
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from db import SessionLocal
from load import load_tests
from run import execute_run
from evaluate import evaluate_run

logger = logging.getLogger("webapp.jobs")


@dataclass
class Job:
    id: str
    params: dict[str, Any]
    status: str = "queued"           # queued | running | done | error
    phase: str = ""
    current: int = 0
    total: int = 0
    error: Optional[str] = None
    run_ids: list[int] = field(default_factory=list)
    log: deque = field(default_factory=lambda: deque(maxlen=400))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def as_dict(self) -> dict[str, Any]:
        pct = int(100 * self.current / self.total) if self.total else 0
        return dict(
            id=self.id,
            status=self.status,
            phase=self.phase,
            current=self.current,
            total=self.total,
            pct=pct,
            error=self.error,
            run_ids=self.run_ids,
            log=list(self.log),
            created_at=self.created_at.isoformat(),
            params=self.params,
        )


class _JobLogHandler(logging.Handler):
    def __init__(self, job: Job) -> None:
        super().__init__()
        self.job = job

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.job.log.append(self.format(record))
        except Exception:
            pass


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._run_worker, daemon=True)
        self._worker.start()

    def submit(self, params: dict[str, Any]) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], params=params)
        with self._lock:
            self._jobs[job.id] = job
        self._queue.put(job.id)
        logger.info("Job %s en file (%s)", job.id, params)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    # -- worker --
    def _run_worker(self) -> None:
        while True:
            job_id = self._queue.get()
            job = self._jobs.get(job_id)
            if job is not None:
                self._execute(job)
            self._queue.task_done()

    def _execute(self, job: Job) -> None:
        handler = _JobLogHandler(job)
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        root = logging.getLogger()
        # Les loggers GEOeval émettent en INFO : s'assurer que le root laisse
        # passer INFO, sinon les records n'atteignent jamais le handler du job.
        if root.level == logging.NOTSET or root.level > logging.INFO:
            root.setLevel(logging.INFO)
        root.addHandler(handler)

        job.status = "running"
        try:
            tested_models: list[str] = job.params["tested_models"]
            judges: list[dict[str, Any]] = job.params["judges"]
            note: Optional[str] = job.params.get("note") or None

            for tm in tested_models:
                # PHASE RUN
                with SessionLocal() as session:
                    tests = load_tests(session, active_only=True, ready_only=True)
                    if not tests:
                        raise ValueError(
                            "Aucun test actif et prêt : active ou crée des tests "
                            "avant de lancer un run."
                        )

                    def run_cb(cur: int, tot: int, detail: str, _tm=tm) -> None:
                        job.phase = f"RUN {_tm} · {detail}"
                        job.current, job.total = cur, tot

                    run_id = execute_run(
                        session,
                        tested_model=tm,
                        tests=tests,
                        run_meta={"note": note} if note else None,
                        progress_cb=run_cb,
                    )
                    session.commit()
                    job.run_ids.append(run_id)

                # PHASE ÉVALUATION
                with SessionLocal() as session:
                    def eval_cb(cur: int, tot: int, detail: str, _tm=tm, _rid=run_id) -> None:
                        job.phase = f"ÉVAL {_tm} (run {_rid}) · {detail}"
                        job.current, job.total = cur, tot

                    evaluate_run(session, run_id=run_id, judges=judges, progress_cb=eval_cb)
                    session.commit()

            job.phase = "terminé"
            job.status = "done"
            logger.info("Job %s terminé (runs %s)", job.id, job.run_ids)
        except Exception as exc:  # noqa: BLE001
            job.status = "error"
            job.error = str(exc)
            logger.exception("Job %s en échec", job.id)
        finally:
            root.removeHandler(handler)


# Singleton process-wide
manager = JobManager()
