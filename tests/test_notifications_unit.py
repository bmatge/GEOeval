"""Règles pures d'E7 : série « toujours faux », défauts plateforme, catalogue des types."""
from __future__ import annotations

from decimal import Decimal

import pytest

from geoeval.web import detectors, notifications


def test_serie_en_cours_sous_le_seuil():
    t = Decimal("5")
    # du plus récent au plus ancien
    assert detectors.streak([(9, Decimal("3")), (8, Decimal("4.9")), (7, Decimal("6")), (6, Decimal("1"))], t) == [9, 8]
    assert detectors.streak([(9, Decimal("5"))], t) == [], "le seuil n'est pas « sous »"
    assert detectors.streak([], t) == []
    assert detectors.streak([(3, None), (2, Decimal("1"))], t) == [], "note absente : série interrompue"


def test_defauts_plateforme(monkeypatch):
    monkeypatch.delenv("GEOEVAL_ALWAYS_WRONG_RUNS", raising=False)
    monkeypatch.delenv("GEOEVAL_ALWAYS_WRONG_THRESHOLD", raising=False)
    assert detectors.platform_defaults() == (3, Decimal("5"))
    monkeypatch.setenv("GEOEVAL_ALWAYS_WRONG_RUNS", "50")
    monkeypatch.setenv("GEOEVAL_ALWAYS_WRONG_THRESHOLD", "6,5")
    assert detectors.platform_defaults() == (20, Decimal("6.5")), "borné à 2–20, virgule acceptée"
    monkeypatch.setenv("GEOEVAL_ALWAYS_WRONG_RUNS", "beaucoup")
    assert detectors.platform_defaults()[0] == 3


@pytest.mark.parametrize("kind", list(notifications.KINDS))
def test_catalogue_des_types(kind):
    k = notifications.KINDS[kind]
    assert k.label and k.description and set(k.roles) <= {"org_admin", "editor", "viewer"} and "org_admin" in k.roles


def test_defauts_email_par_type():
    """Arbitrage E7 : email par défaut pour budget, contrats, échec ; in-app seul pour « toujours faux »."""
    assert {k for k, v in notifications.KINDS.items() if v.email_default} == {
        "budget_threshold", "job_failed", "contract_expiring", "contract_expired"}
    assert notifications.KINDS["always_wrong"].email_default is False
