"""
Processus worker GEOeval (ADR-088, lot 1.2).

    python -m geoeval.worker.main

Boucle : récupération des jobs interrompus (battement de cœur périmé), tick du
planificateur sous verrou, réclamation d'un job (`FOR UPDATE SKIP LOCKED`),
exécution. Plusieurs réplicas peuvent tourner en parallèle sans coordination
externe : PostgreSQL est la file.

Arrêt gracieux : SIGTERM / SIGINT demandent l'arrêt ; le modèle en cours
(RUN + ÉVALUATION) se termine, les modèles restants ne démarrent pas et le job
passe en `error` avec un motif explicite. Prévoir un délai d'arrêt suffisant
côté orchestrateur (`stop_grace_period` du compose).

Mode inline (dev local, tests, repli VibeLab) : `GEOEVAL_INLINE_WORKER=1` fait
tourner cette même boucle dans un thread du processus web.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
import time
from typing import Optional

from sqlalchemy import text

from geoeval.db.session import SessionLocal
from geoeval.worker import jobs, scheduler

logger = logging.getLogger("geoeval.worker.main")

POLL_SECONDS = 2.0           # attente quand la file est vide
RECOVER_EVERY_SECONDS = 30.0  # fréquence de recover_stale
SCHEMA_WAIT_SECONDS = 300     # attente max du schéma (migrations faites par le web)


def inline_worker_enabled() -> bool:
    return os.environ.get("GEOEVAL_INLINE_WORKER", "0").strip().lower() in ("1", "true", "yes")


def wait_for_schema(timeout: float = SCHEMA_WAIT_SECONDS, stop: Optional[threading.Event] = None) -> bool:
    """Attend que la base réponde et que la table `jobs` existe (créée par
    l'entrypoint web : init_db → migrations). Renvoie False si abandon."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with SessionLocal() as session:
                session.execute(text("SELECT 1 FROM jobs LIMIT 0"))
            return True
        except Exception as exc:  # noqa: BLE001
            if time.monotonic() >= deadline or (stop is not None and stop.is_set()):
                logger.error("schéma indisponible après %ss : %s", timeout, exc)
                return False
            logger.info("en attente de la base / du schéma (%s)…", type(exc).__name__)
            if stop is not None:
                stop.wait(3)
            else:
                time.sleep(3)


def run_forever(stop: threading.Event, *, worker_id: Optional[str] = None) -> None:
    """Boucle principale. S'arrête quand `stop` est levé, après le job en cours."""
    worker_id = worker_id or jobs.worker_identity()
    if not wait_for_schema(stop=stop):
        return
    logger.info("worker %s prêt (poll %ss, scheduler %ss)", worker_id, POLL_SECONDS, scheduler.POLL_SECONDS)

    last_recover = 0.0
    last_tick = 0.0
    while not stop.is_set():
        try:
            now = time.monotonic()
            if now - last_recover >= RECOVER_EVERY_SECONDS:
                with SessionLocal() as session:
                    jobs.recover_stale(session)
                last_recover = now
            if now - last_tick >= scheduler.POLL_SECONDS:
                with SessionLocal() as session:
                    n = scheduler.tick_if_leader(session)
                if n:
                    logger.info("scheduler : %d planification(s) mise(s) en file", n)
                last_tick = now

            with SessionLocal() as session:
                job = jobs.claim_next(session, worker_id)
            if job is not None:
                logger.info("job %s réclamé par %s", job.id, worker_id)
                jobs.execute(job.id, stop_event=stop)
                continue  # on enchaîne sans attendre
        except Exception:  # noqa: BLE001 — le worker ne doit jamais mourir sur une itération
            logger.exception("itération du worker en échec")
        stop.wait(POLL_SECONDS)
    logger.info("worker %s arrêté", worker_id)


# ---------------------------------------------------------------------
# Mode inline (thread dans le processus web)
# ---------------------------------------------------------------------
_inline_lock = threading.Lock()
_inline_started = False


def start_inline_thread() -> bool:
    """Démarre la boucle worker dans un thread démon (idempotent)."""
    global _inline_started
    with _inline_lock:
        if _inline_started:
            return False
        threading.Thread(
            target=run_forever, args=(threading.Event(),), daemon=True, name="geoeval-inline-worker"
        ).start()
        _inline_started = True
        logger.warning("worker inline démarré dans le processus web (GEOEVAL_INLINE_WORKER=1) — dev / repli")
        return True


# ---------------------------------------------------------------------
# Processus autonome
# ---------------------------------------------------------------------
def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    stop = threading.Event()

    def _on_signal(signum, _frame) -> None:
        logger.warning("signal %s reçu : arrêt demandé, fin du job en cours", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    run_forever(stop)


if __name__ == "__main__":
    main()
