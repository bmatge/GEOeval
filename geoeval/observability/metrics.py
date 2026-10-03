"""
Métriques Prometheus (registre par défaut de prometheus-client).

Web : requêtes HTTP par route et statut, durée. Worker : jobs exécutés et
durée, dernier signe de vie. Communs : appels LLM par famille et issue, file
de jobs par statut (lue en base au moment du scrape).
"""
from __future__ import annotations

import logging
import time
from typing import Iterable

from prometheus_client import REGISTRY, CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily

logger = logging.getLogger("geoeval.observability.metrics")

HTTP_REQUESTS = Counter("geoeval_http_requests_total", "Requêtes HTTP", ["method", "route", "status"])
HTTP_DURATION = Histogram(
    "geoeval_http_request_duration_seconds", "Durée des requêtes HTTP", ["method", "route"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
JOBS_EXECUTED = Counter("geoeval_jobs_total", "Jobs exécutés par le worker, par issue", ["status"])
JOB_DURATION = Histogram(
    "geoeval_job_duration_seconds", "Durée d'exécution d'un job",
    buckets=(1, 5, 15, 60, 300, 900, 1800, 3600, 7200),
)
LLM_CALLS = Counter("geoeval_llm_calls_total", "Appels LLM par famille et issue", ["family", "outcome"])
LLM_DURATION = Histogram(
    "geoeval_llm_call_duration_seconds", "Durée d'un appel LLM (hors attente de retry)", ["family"],
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300),
)
NOTIFICATIONS = Counter("geoeval_notifications_total", "Notifications émises (par destinataire), par type", ["kind"])
WORKER_ALIVE = Gauge("geoeval_worker_last_alive_timestamp_seconds", "Dernier signe de vie du worker (epoch)")


def touch_worker_alive() -> None:
    WORKER_ALIVE.set(time.time())


def worker_alive_age() -> float:
    """Secondes depuis le dernier signe de vie (inf si jamais)."""
    last = WORKER_ALIVE._value.get()  # noqa: SLF001 — lecture locale du gauge
    return float("inf") if not last else time.time() - last


class JobsQueueCollector:
    """Jauge `geoeval_jobs_in_queue{status}` lue en base à chaque scrape."""

    def collect(self) -> Iterable[GaugeMetricFamily]:
        g = GaugeMetricFamily("geoeval_jobs_in_queue", "Jobs par statut (table jobs)", labels=["status"])
        try:
            from sqlalchemy import func, select

            from geoeval.db.models import Job
            from geoeval.db.session import SessionLocal

            with SessionLocal() as session:
                rows = session.execute(select(Job.status, func.count()).group_by(Job.status)).all()
            counts = {status: int(n) for status, n in rows}
        except Exception:  # noqa: BLE001 — un scrape ne doit jamais casser
            logger.debug("lecture de la file de jobs impossible pour /metrics", exc_info=True)
            return
        for status in ("queued", "running", "done", "error"):
            g.add_metric([status], counts.get(status, 0))
        yield g

    def describe(self):
        return []


class BudgetCollector:
    """Jauges budgétaires par entité et période, lues en base au scrape (E3) :
    `geoeval_budget_spent_eur`, `geoeval_budget_cap_eur`, `geoeval_budget_ratio`
    (dépense consolidée / plafond) — base de l'alerting d'exploitation."""

    def collect(self) -> Iterable[GaugeMetricFamily]:
        spent = GaugeMetricFamily("geoeval_budget_spent_eur", "Dépense consolidée sur la période (€)", labels=["org", "period"])
        cap = GaugeMetricFamily("geoeval_budget_cap_eur", "Plafond de la période (€)", labels=["org", "period"])
        ratio = GaugeMetricFamily("geoeval_budget_ratio", "Dépense consolidée / plafond", labels=["org", "period"])
        try:
            from sqlalchemy import select

            from geoeval.db.models import Budget, Organization
            from geoeval.db.session import SessionLocal
            from geoeval.web import budget

            with SessionLocal() as session:
                owners = session.execute(
                    select(Organization).join(Budget, Budget.organization_id == Organization.id)
                ).scalars().all()
                rows = [c for o in owners for c in budget.constraints_for_owner(session, o)]
        except Exception:  # noqa: BLE001 — un scrape ne doit jamais casser
            logger.debug("lecture des budgets impossible pour /metrics", exc_info=True)
            return
        for c in rows:
            labels = [c.owner.slug, c.period]
            spent.add_metric(labels, float(c.spent_eur))
            cap.add_metric(labels, float(c.cap_eur))
            if c.ratio is not None:
                ratio.add_metric(labels, float(c.ratio))
        yield spent
        yield cap
        yield ratio

    def describe(self):
        return []


_registered = False


def register_collectors() -> None:
    global _registered
    if _registered:
        return
    REGISTRY.register(JobsQueueCollector())
    REGISTRY.register(BudgetCollector())
    _registered = True


def render() -> tuple[bytes, str]:
    """Exposition texte Prometheus + son Content-Type."""
    register_collectors()
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
