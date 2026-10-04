"""Calibration et comparaison de rejugement : calculs purs (sans base)."""
from __future__ import annotations

import pytest

from geoeval.web import calibration


def _v(rs, rl="conforme", cs=None, cl="conforme"):
    return dict(response_score=rs, response_label=rl, citation_score=cs, citation_label=cl)


def test_regroupement_annotateurs_et_repetitions():
    rows = [(1, 10, "conforme", 8, "partiel", 6), (1, 10, "partiel", 6, "partiel", None),
            (1, 10, "conforme", 7, "conforme", 4), (2, 10, None, None, None, None)]
    out = calibration._collapse(rows)
    assert out[(1, 10)] == dict(response_label="conforme", citation_label="partiel", response_score=7.0, citation_score=5.0)
    assert out[(2, 10)] == dict(response_label="", citation_label="", response_score=None, citation_score=None)


def test_metriques_d_accord():
    gold = [_v(2, "non_conforme", 2), _v(5, "partiel", 5), _v(9, "conforme", 9)]
    m = calibration.metrics(gold, [_v(3, "non_conforme", 1), _v(6, "partiel", 6), _v(8, "conforme", 9)])
    assert m["n_pairs"] == 3 and m["response_spearman"] == pytest.approx(1.0) and m["response_kappa"] == pytest.approx(1.0)
    assert m["response_mae"] == pytest.approx(1.0) and m["citation_spearman"] == pytest.approx(1.0)
    inv = calibration.metrics(gold, [_v(9, "conforme"), _v(5, "partiel"), _v(2, "non_conforme")])
    assert inv["response_spearman"] == pytest.approx(-1.0) and inv["citation_spearman"] is None
    assert calibration.metrics([], [])["response_kappa"] is None


def test_format():
    assert calibration.fmt(None) == "—" and calibration.fmt(0.123456) == "0.12" and calibration.fmt(-1) == "-1.00"
