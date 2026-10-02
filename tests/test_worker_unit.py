"""Worker (lot 1.2) — helpers sans base."""
from __future__ import annotations

from datetime import datetime, timezone

from geoeval.db.models import Job
from geoeval.worker import jobs
from geoeval.worker.main import inline_worker_enabled


def test_as_dict_forme_attendue_par_les_gabarits():
    job = Job(id="abc123", organization_id=1, status="running", priority=0,
              params={"tested_models": ["m1"], "organization_id": 1}, phase="RUN m1 · test 3",
              current=3, total=12, run_ids=[41], attempt=1, worker_id="w1",
              created_at=datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc))
    d = jobs.as_dict(job, log=["l1", "l2"])
    assert d["id"] == "abc123" and d["status"] == "running"
    assert d["pct"] == 25 and d["current"] == 3 and d["total"] == 12
    assert d["run_ids"] == [41] and d["log"] == ["l1", "l2"]
    assert d["params"]["tested_models"] == ["m1"]
    assert d["created_at"] == "2026-10-02T12:00:00+00:00"


def test_as_dict_sans_total_ni_log():
    job = Job(id="x", organization_id=1, status="queued", params={}, phase="", current=0, total=0, run_ids=None)
    d = jobs.as_dict(job)
    assert d["pct"] == 0 and d["run_ids"] == [] and d["log"] == [] and d["created_at"] is None


def test_inline_worker_desactive_par_defaut(monkeypatch):
    monkeypatch.delenv("GEOEVAL_INLINE_WORKER", raising=False)
    assert inline_worker_enabled() is False
    for v in ("1", "true", "YES"):
        monkeypatch.setenv("GEOEVAL_INLINE_WORKER", v)
        assert inline_worker_enabled() is True
    monkeypatch.setenv("GEOEVAL_INLINE_WORKER", "0")
    assert inline_worker_enabled() is False


def test_worker_identity_et_job_id():
    assert ":" in jobs.worker_identity()
    assert len(jobs.new_job_id()) == 12 and jobs.new_job_id() != jobs.new_job_id()
