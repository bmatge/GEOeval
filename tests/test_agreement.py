"""Kappa de Cohen et Spearman (geoeval/web/agreement.py) — calculs à la main."""
from __future__ import annotations

import pytest

from geoeval.web.agreement import cohen_kappa, spearman_rho


def test_kappa_accord_parfait():
    assert cohen_kappa(["a", "b", "a"], ["a", "b", "a"]) == pytest.approx(1.0)


def test_kappa_desaccord_total_deux_classes():
    # p_o = 0 ; p_e = 0.5 → kappa = -1
    assert cohen_kappa(["a", "b"], ["b", "a"]) == pytest.approx(-1.0)


def test_kappa_valeur_connue():
    # Exemple classique : p_o = 0.7, p_e = 0.5 → kappa = 0.4
    a = ["x"] * 5 + ["y"] * 5
    b = ["x"] * 4 + ["y"] + ["y"] * 3 + ["x"] * 2
    assert cohen_kappa(a, b) == pytest.approx(0.4)


def test_kappa_une_seule_categorie():
    assert cohen_kappa(["a", "a"], ["a", "a"]) == 1.0


def test_kappa_entrees_invalides():
    assert cohen_kappa([], []) is None
    assert cohen_kappa(["a"], ["a", "b"]) is None


def test_spearman_monotone():
    assert spearman_rho([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman_rho([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)


def test_spearman_egalites_rangs_moyens():
    # Rangs a : [1, 2.5, 2.5, 4] ; b identique → rho = 1
    assert spearman_rho([1, 2, 2, 3], [5, 6, 6, 7]) == pytest.approx(1.0)


def test_spearman_constante_ou_trop_court():
    assert spearman_rho([1, 1, 1], [1, 2, 3]) is None
    assert spearman_rho([1], [1]) is None
    assert spearman_rho([], []) is None
    assert spearman_rho([1, 2], [1]) is None
