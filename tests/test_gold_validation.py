"""Validation des lignes du gold set CSV (geoeval/web/gold.py)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from geoeval.web.gold import _validate_label, _validate_score


def test_score_virgule_francaise():
    assert _validate_score("7,5") == Decimal("7.50")


def test_score_bornes():
    assert _validate_score("0") == Decimal("0.00")
    assert _validate_score("10") == Decimal("10.00")
    for bad in ("-0.01", "10.5", "abc", ""):
        with pytest.raises(ValueError):
            _validate_score(bad)


def test_label_normalise():
    assert _validate_label("  Conforme ", "response_label") == "conforme"
    assert _validate_label("NON_CONFORME", "citation_label") == "non_conforme"


def test_label_inconnu():
    with pytest.raises(ValueError, match="response_label"):
        _validate_label("moyen", "response_label")
