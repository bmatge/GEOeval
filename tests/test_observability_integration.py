"""Observabilité (lot 1.5) — sondes web et worker, request_id, métriques HTTP et jobs."""
from __future__ import annotations

import urllib.error
import urllib.request

import pytest
from sqlalchemy import text

from geoeval.observability import health, metrics
from geoeval.observability.context import job_id_var
from geoeval.worker import jobs
from geoeval.worker.health import start_health_server
from tests.conftest import TEST_ORG_SLUG

pytestmark = pytest.mark.integration


def _sample(body: str, prefix: str) -> list[str]:
    return [line for line in body.splitlines() if line.startswith(prefix)]


def test_sondes_web(anonymous_client):
    r = anonymous_client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok", "service": "web"}
    r = anonymous_client.get("/readyz")
    assert r.status_code == 200 and r.json()["database"] == "ok"
    assert "/healthz" not in anonymous_client.get("/openapi.json").json()["paths"]


def test_readyz_503_si_base_indisponible(anonymous_client, monkeypatch):
    monkeypatch.setattr(health, "check_db", lambda: (False, {"database": "unavailable", "error": "OperationalError"}))
    r = anonymous_client.get("/readyz")
    assert r.status_code == 503 and r.json()["status"] == "unavailable"


def test_request_id_propage_ou_genere(anonymous_client):
    r = anonymous_client.get(f"/o/{TEST_ORG_SLUG}/dashboard", headers={"X-Request-ID": "ingress-42"})
    assert r.status_code == 200 and r.headers["x-request-id"] == "ingress-42"
    r = anonymous_client.get(f"/o/{TEST_ORG_SLUG}/dashboard")
    assert len(r.headers["x-request-id"]) == 32


def test_metriques_http_par_gabarit_de_route(anonymous_client):
    anonymous_client.get(f"/o/{TEST_ORG_SLUG}/runs")
    anonymous_client.get("/api/v1/orgs/inexistante")
    body = anonymous_client.get("/metrics").text
    assert any('route="/o/{org_slug}/runs",status="200"' in line for line in _sample(body, "geoeval_http_requests_total"))
    assert any('route="/api/v1/orgs/{org_slug}",status="404"' in line for line in _sample(body, "geoeval_http_requests_total"))
    assert _sample(body, "geoeval_http_request_duration_seconds_bucket")
    assert any(line.startswith('geoeval_jobs_in_queue{status="queued"}') for line in body.splitlines())


def test_metriques_jobs_et_contexte_job(db_session, test_org, monkeypatch):
    db_session.execute(text("DELETE FROM job_logs"))
    db_session.execute(text("DELETE FROM jobs"))
    db_session.commit()
    seen = {}

    def fake_execute_run(session, *, tested_model, tests, progress_cb=None, **kw):
        seen["job_id_in_context"] = job_id_var.get()
        return 7

    monkeypatch.setattr(jobs, "select_tests", lambda session, **kw: [object()])
    monkeypatch.setattr(jobs, "execute_run", fake_execute_run)
    monkeypatch.setattr(jobs, "evaluate_run", lambda session, **kw: None)
    monkeypatch.setattr(jobs, "HEARTBEAT_SECONDS", 3600)

    before = metrics.JOBS_EXECUTED.labels("done")._value.get()
    job = jobs.submit(db_session, dict(organization_id=test_org["id"], tested_models=["m"], judges=[]))
    jobs.claim_next(db_session, "w-obs")
    jobs.execute(job.id)
    assert seen["job_id_in_context"] == job.id, "job_id posé dans le contexte pendant l'exécution"
    assert job_id_var.get() is None, "contexte nettoyé après le job"
    assert metrics.JOBS_EXECUTED.labels("done")._value.get() == before + 1


def test_serveur_de_sante_du_worker():
    server = start_health_server(port=0)
    try:
        port = server.server_address[1]
        metrics.touch_worker_alive()
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
            assert r.status == 200 and b'"service": "worker"' in r.read()
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=5) as r:
            assert r.status == 200
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as r:
            assert r.status == 200 and b"geoeval_worker_last_alive_timestamp_seconds" in r.read()
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/autre", timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_worker_healthz_503_si_fige():
    server = start_health_server(port=0)
    try:
        metrics.WORKER_ALIVE.set(1.0)  # epoch 1970 : figé depuis toujours
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/healthz", timeout=5)
            raise AssertionError("503 attendu")
        except urllib.error.HTTPError as exc:
            assert exc.code == 503
    finally:
        metrics.touch_worker_alive()
        server.shutdown()
        server.server_close()
