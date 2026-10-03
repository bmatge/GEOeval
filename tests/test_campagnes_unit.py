"""Règles pures d'E8 : écarts des résultats de campagne."""
from __future__ import annotations

from types import SimpleNamespace

from geoeval.web import campaigns


def _row(**kw):
    base = dict(org=SimpleNamespace(id=1, path="/1/"), model_version="m", n_runs=1, last_run_id=1, last_at=None,
                last_response=None, last_citation=None)
    base.update(kw)
    return campaigns.ResultRow(**base)


def test_ecarts_entre_deux_executions():
    r = _row(last_response=7.5, prev_response=6.25, last_citation=4.0, prev_citation=5.0)
    assert r.delta_response == 1.25 and r.delta_citation == -1.0


def test_ecart_absent_sans_execution_precedente_ou_sans_note():
    assert _row(last_response=7.0).delta_response is None
    assert _row(last_response=None, prev_response=6.0).delta_response is None


def test_libelles_des_etats():
    assert set(campaigns.STATUS_LABELS) == {"draft", "active", "closed"}
