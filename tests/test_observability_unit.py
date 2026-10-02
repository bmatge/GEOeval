"""Observabilité (lot 1.5) — formatter JSON, contexte, métriques LLM, sans base."""
from __future__ import annotations

import io
import json
import logging

import pytest

from geoeval.core import llm_clients
from geoeval.observability import logs, metrics
from geoeval.observability.context import job_id_var, llm_family_var, request_id_var


@pytest.fixture()
def json_stream(monkeypatch):
    monkeypatch.setenv("GEOEVAL_LOG_FORMAT", "json")
    stream = io.StringIO()
    logs.configure_logging("test", stream=stream, force=True)
    yield stream
    logs.configure_logging("test", stream=io.StringIO(), force=True)


def test_json_formatter_champs_et_contexte(json_stream):
    tok_r = request_id_var.set("req-1")
    tok_j = job_id_var.set("job-9")
    try:
        logging.getLogger("geoeval.test").info("bonjour %s", "monde", extra={"custom": 42})
    finally:
        request_id_var.reset(tok_r)
        job_id_var.reset(tok_j)
    line = json_stream.getvalue().strip().splitlines()[-1]
    rec = json.loads(line)
    assert rec["msg"] == "bonjour monde" and rec["level"] == "INFO" and rec["logger"] == "geoeval.test"
    assert rec["service"] == "test" and rec["request_id"] == "req-1" and rec["job_id"] == "job-9" and rec["custom"] == 42
    assert rec["ts"].endswith("+00:00")


def test_json_formatter_exception(json_stream):
    try:
        raise ValueError("boum")
    except ValueError:
        logging.getLogger("geoeval.test").exception("échec")
    rec = json.loads(json_stream.getvalue().strip().splitlines()[-1])
    assert "ValueError: boum" in rec["exc"] and rec["level"] == "ERROR"


def test_format_texte_avec_contexte(monkeypatch):
    monkeypatch.setenv("GEOEVAL_LOG_FORMAT", "text")
    stream = io.StringIO()
    logs.configure_logging("test", stream=stream, force=True)
    tok = request_id_var.set("req-2")
    try:
        logging.getLogger("geoeval.test").warning("attention")
    finally:
        request_id_var.reset(tok)
    assert "| WARNING | geoeval.test | attention | request_id=req-2" in stream.getvalue()
    logs.configure_logging("test", stream=io.StringIO(), force=True)


def test_log_format_auto(monkeypatch):
    monkeypatch.setenv("GEOEVAL_LOG_FORMAT", "json")
    assert logs.log_format() == "json"
    monkeypatch.setenv("GEOEVAL_LOG_FORMAT", "TEXT")
    assert logs.log_format() == "text"
    monkeypatch.delenv("GEOEVAL_LOG_FORMAT", raising=False)
    assert logs.log_format() in ("json", "text")


def _counter(name: str, **labels) -> float:
    for metric in metrics.REGISTRY.collect():
        if metric.name == name.removesuffix("_total"):
            for s in metric.samples:
                if s.name == name and all(s.labels.get(k) == v for k, v in labels.items()):
                    return s.value
    return 0.0


def test_metriques_llm_par_famille(monkeypatch):
    monkeypatch.setattr(llm_clients, "_sleep_with_jitter", lambda s: None)
    monkeypatch.setattr(llm_clients.time, "sleep", lambda s: None)
    tok = llm_family_var.set("testfam")
    try:
        before_ok = _counter("geoeval_llm_calls_total", family="testfam", outcome="ok")
        before_retry = _counter("geoeval_llm_calls_total", family="testfam", outcome="retry")
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            if calls["n"] < 2:
                raise RuntimeError("503")
            return "ok"

        assert llm_clients.call_with_retry(fn, retry_exceptions=(Exception,), max_retries=3) == "ok"
        assert _counter("geoeval_llm_calls_total", family="testfam", outcome="ok") == before_ok + 1
        assert _counter("geoeval_llm_calls_total", family="testfam", outcome="retry") == before_retry + 1

        class Err(Exception):
            status_code = 401

        before_nr = _counter("geoeval_llm_calls_total", family="testfam", outcome="non_retryable")
        with pytest.raises(llm_clients.LLMCallError):
            llm_clients.call_with_retry(lambda: (_ for _ in ()).throw(Err("bad key")), retry_exceptions=(Exception,))
        assert _counter("geoeval_llm_calls_total", family="testfam", outcome="non_retryable") == before_nr + 1
    finally:
        llm_family_var.reset(tok)


def test_worker_alive_age():
    metrics.touch_worker_alive()
    assert metrics.worker_alive_age() < 5


def test_render_expose_le_format_prometheus():
    body, content_type = metrics.render()
    assert content_type.startswith("text/plain") and b"geoeval_http_requests_total" in body
