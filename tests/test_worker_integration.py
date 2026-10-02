"""Worker (lot 1.2) — file persistée, réclamation, exécution, planificateur, API.

Les appels LLM sont remplacés par des doublures : on teste la mécanique, pas
les modèles.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from geoeval.db.models import Job, JobLog, Perimeter, ScheduledRun
from geoeval.worker import jobs, scheduler
from tests.conftest import TEST_ORG_SLUG

pytestmark = pytest.mark.integration


def _params(org_id: int, models=("modele-a",), **extra):
    return dict(organization_id=org_id, tested_models=list(models), judges=[{"model": "juge", "repeats": 1}], **extra)


@pytest.fixture()
def clean_jobs(db_session):
    db_session.execute(text("DELETE FROM job_logs"))
    db_session.execute(text("DELETE FROM jobs"))
    db_session.commit()
    yield


# ---------------------------------------------------------------------
# File
# ---------------------------------------------------------------------
def test_submit_list_et_isolation_org(db_session, test_org, clean_jobs):
    job = jobs.submit(db_session, _params(test_org["id"]))
    assert job.status == "queued" and job.attempt == 0
    assert [j.id for j in jobs.list_for_org(db_session, test_org["id"])] == [job.id]
    assert jobs.get_for_org(db_session, test_org["id"], job.id) is not None
    assert jobs.get_for_org(db_session, test_org["id"] + 999, job.id) is None
    assert jobs.list_for_org(db_session, test_org["id"] + 999) == []


def test_claim_respecte_priorite_puis_anciennete_et_ne_rend_qu_une_fois(db_session, test_org, clean_jobs):
    j1 = jobs.submit(db_session, _params(test_org["id"], note="premier"))
    j2 = jobs.submit(db_session, _params(test_org["id"], note="second"))
    j3 = jobs.submit(db_session, _params(test_org["id"], note="urgent"), priority=10)

    c1 = jobs.claim_next(db_session, "w1")
    assert c1.id == j3.id and c1.status == "running" and c1.worker_id == "w1" and c1.attempt == 1
    assert c1.claimed_at is not None and c1.heartbeat_at is not None
    assert jobs.claim_next(db_session, "w2").id == j1.id
    assert jobs.claim_next(db_session, "w1").id == j2.id
    assert jobs.claim_next(db_session, "w1") is None


def test_recover_stale_marque_interrompu_sans_relance(db_session, test_org, clean_jobs):
    stale = jobs.submit(db_session, _params(test_org["id"]))
    fresh = jobs.submit(db_session, _params(test_org["id"]))
    jobs.claim_next(db_session, "w-mort")
    jobs.claim_next(db_session, "w-vivant")
    old = datetime.now(timezone.utc) - timedelta(seconds=jobs.STALE_AFTER_SECONDS + 60)
    db_session.execute(text("UPDATE jobs SET heartbeat_at = :t WHERE id = :id"), {"t": old, "id": stale.id})
    db_session.commit()

    assert jobs.recover_stale(db_session) == 1
    db_session.expire_all()
    s, f = db_session.get(Job, stale.id), db_session.get(Job, fresh.id)
    assert s.status == "error" and s.error == jobs.INTERRUPTED_MESSAGE and s.phase == "interrompu"
    assert f.status == "running"
    assert jobs.claim_next(db_session, "w3") is None, "un job interrompu n'est jamais remis en file"


# ---------------------------------------------------------------------
# Exécution avec doublures LLM
# ---------------------------------------------------------------------
@pytest.fixture()
def fake_core(monkeypatch):
    """Remplace load_tests / execute_run / evaluate_run dans le module jobs."""
    calls = {"runs": [], "evals": []}
    counter = {"run_id": 1000}

    def fake_load_tests(session, **kw):
        return [object(), object(), object()]

    def fake_execute_run(session, *, tested_model, tests, progress_cb=None, **kw):
        logging.getLogger("geoeval.core.run").info("run %s : %d tests", tested_model, len(tests))
        for i in range(1, len(tests) + 1):
            if progress_cb:
                progress_cb(i, len(tests), f"test {i}")
        counter["run_id"] += 1
        calls["runs"].append(tested_model)
        return counter["run_id"]

    def fake_evaluate_run(session, *, run_id, judges, progress_cb=None, **kw):
        if progress_cb:
            progress_cb(1, 1, "juge")
        calls["evals"].append(run_id)

    monkeypatch.setattr(jobs, "load_tests", fake_load_tests)
    monkeypatch.setattr(jobs, "execute_run", fake_execute_run)
    monkeypatch.setattr(jobs, "evaluate_run", fake_evaluate_run)
    monkeypatch.setattr(jobs, "HEARTBEAT_SECONDS", 3600)  # pas de thread bavard pendant le test
    return calls


def test_execute_job_complet(db_session, test_org, clean_jobs, fake_core):
    job = jobs.submit(db_session, _params(test_org["id"], models=("m1", "m2")))
    jobs.claim_next(db_session, "w1")

    jobs.execute(job.id)

    db_session.expire_all()
    j = db_session.get(Job, job.id)
    assert j.status == "done" and j.phase == "terminé" and j.finished_at is not None
    assert j.run_ids == [1001, 1002] and fake_core["runs"] == ["m1", "m2"] and fake_core["evals"] == [1001, 1002]
    assert j.total == 1 and j.current == 1  # dernière progression = phase ÉVAL
    logs = jobs.tail_logs(db_session, job.id)
    assert any("run m1 : 3 tests" in line for line in logs)
    assert any("terminé" in line for line in logs)
    assert db_session.execute(select(JobLog).where(JobLog.job_id == job.id)).scalars().first() is not None


def test_execute_job_en_echec(db_session, test_org, clean_jobs, fake_core, monkeypatch):
    def boom(session, **kw):
        raise RuntimeError("clé API invalide")

    monkeypatch.setattr(jobs, "execute_run", boom)
    job = jobs.submit(db_session, _params(test_org["id"]))
    jobs.claim_next(db_session, "w1")
    jobs.execute(job.id)
    db_session.expire_all()
    j = db_session.get(Job, job.id)
    assert j.status == "error" and "clé API invalide" in j.error and j.run_ids == []


def test_execute_job_sans_question(db_session, test_org, clean_jobs, fake_core, monkeypatch):
    monkeypatch.setattr(jobs, "load_tests", lambda session, **kw: [])
    job = jobs.submit(db_session, _params(test_org["id"]))
    jobs.claim_next(db_session, "w1")
    jobs.execute(job.id)
    db_session.expire_all()
    assert "Aucune question" in db_session.get(Job, job.id).error


def test_arret_demande_interrompt_entre_deux_modeles(db_session, test_org, clean_jobs, fake_core):
    job = jobs.submit(db_session, _params(test_org["id"], models=("m1", "m2", "m3")))
    jobs.claim_next(db_session, "w1")
    stop = threading.Event()
    stop.set()  # arrêt demandé avant même le premier modèle → rien ne démarre

    jobs.execute(job.id, stop_event=stop)
    db_session.expire_all()
    j = db_session.get(Job, job.id)
    assert j.status == "error" and j.phase == "interrompu" and "Interrompu" in j.error
    assert j.run_ids == [] and fake_core["runs"] == []


def test_arret_apres_le_modele_en_cours(db_session, test_org, clean_jobs, fake_core, monkeypatch):
    """Le stop arrive pendant m1 : m1 se termine (RUN + ÉVAL), m2 ne démarre pas."""
    stop = threading.Event()
    real_eval = jobs.evaluate_run

    def eval_then_stop(session, **kw):
        real_eval(session, **kw)
        stop.set()

    monkeypatch.setattr(jobs, "evaluate_run", eval_then_stop)
    job = jobs.submit(db_session, _params(test_org["id"], models=("m1", "m2")))
    jobs.claim_next(db_session, "w1")
    jobs.execute(job.id, stop_event=stop)
    db_session.expire_all()
    j = db_session.get(Job, job.id)
    assert j.status == "error" and "avant le modèle m2" in j.error
    assert j.run_ids == [1001] and fake_core["runs"] == ["m1"]


# ---------------------------------------------------------------------
# Planificateur sous verrou
# ---------------------------------------------------------------------
@pytest.fixture()
def perimeter(db_session, test_org):
    p = db_session.execute(select(Perimeter).where(Perimeter.organization_id == test_org["id"])).scalars().first()
    if p is None:
        p = Perimeter(organization_id=test_org["id"], name="Site CI", slug="site-ci", kind="site")
        db_session.add(p)
        db_session.commit()
    return p


def test_tick_met_en_file_les_planifications_echues(db_session, test_org, clean_jobs, perimeter):
    db_session.execute(text("DELETE FROM scheduled_runs WHERE organization_id = :o"), {"o": test_org["id"]})
    now = datetime.now(timezone.utc)
    once = ScheduledRun(organization_id=test_org["id"], perimeter_id=perimeter.id, name="une fois",
                        tested_models=["m1"], judges=[{"model": "j", "repeats": 1}], test_ids=None,
                        schedule_kind="once", schedule_config={"at": "2026-01-01T08:00"}, enabled=True,
                        next_run_at=now - timedelta(minutes=1))
    daily = ScheduledRun(organization_id=test_org["id"], perimeter_id=perimeter.id, name="quotidien",
                         tested_models=["m2"], judges=[{"model": "j", "repeats": 1}], test_ids=[1, 2],
                         schedule_kind="daily", schedule_config={"time": "09:00"}, enabled=True,
                         next_run_at=now - timedelta(minutes=1))
    future = ScheduledRun(organization_id=test_org["id"], perimeter_id=perimeter.id, name="plus tard",
                          tested_models=["m3"], judges=[], test_ids=None,
                          schedule_kind="daily", schedule_config={"time": "09:00"}, enabled=True,
                          next_run_at=now + timedelta(hours=1))
    db_session.add_all([once, daily, future])
    db_session.commit()

    assert scheduler.tick_if_leader(db_session) == 2
    db_session.expire_all()
    queued = jobs.list_for_org(db_session, test_org["id"])
    assert sorted(j.params["tested_models"][0] for j in queued) == ["m1", "m2"]
    assert once.enabled is False and once.next_run_at is None and once.last_job_id in {j.id for j in queued}
    assert daily.enabled is True and daily.next_run_at > now and daily.last_job_id in {j.id for j in queued}
    assert future.last_job_id is None
    # Second tick : plus rien d'échu.
    assert scheduler.tick_if_leader(db_session) == 0


def test_tick_verrou_un_seul_leader(db_session_factory, test_org, clean_jobs):
    """Une transaction qui tient le verrou consultatif fait sauter le tick concurrent."""
    with db_session_factory() as holder, db_session_factory() as other:
        got = holder.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": scheduler.SCHEDULER_LOCK_KEY}).scalar_one()
        assert got is True
        assert scheduler.tick_if_leader(other) is None
        holder.rollback()  # libère
        assert scheduler.tick_if_leader(other) == 0


# ---------------------------------------------------------------------
# UI / API
# ---------------------------------------------------------------------
def test_api_et_pages_jobs(client, db_session, test_org, clean_jobs):
    job = jobs.submit(db_session, _params(test_org["id"]))
    db_session.add(JobLog(job_id=job.id, level="INFO", message="bonjour"))
    db_session.commit()

    r = client.get(f"/o/{TEST_ORG_SLUG}/api/jobs/{job.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued" and body["log"] == ["bonjour"] and body["pct"] == 0
    assert client.get(f"/o/{TEST_ORG_SLUG}/api/jobs/inexistant").status_code == 404
    assert client.get(f"/o/{TEST_ORG_SLUG}/jobs/{job.id}").status_code == 200
    r = client.get(f"/o/{TEST_ORG_SLUG}/jobs")
    assert r.status_code == 200 and job.id in r.text


def test_api_job_autre_org_404(client, db_session, test_org, clean_jobs):
    from geoeval.web import tenancy

    other = tenancy.get_org_by_slug(db_session, "autre-org-ci") or tenancy.create_org(db_session, name="Autre", slug="autre-org-ci")
    job = jobs.submit(db_session, _params(other.id))
    assert client.get(f"/o/{TEST_ORG_SLUG}/api/jobs/{job.id}").status_code == 404
