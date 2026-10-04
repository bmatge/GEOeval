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
    assert k.label and k.description and set(k.roles) <= {"org_admin", "editor", "viewer"}
    assert k.default_mode in notifications.MODES
    assert not k.roles or "org_admin" in k.roles, "un type par rôle notifie toujours les administrateurs"


def test_defauts_email_par_type():
    """Arbitrages E7 : email immédiat par défaut pour budget, contrats, échec ; application seule pour le reste."""
    assert {k for k, v in notifications.KINDS.items() if v.default_mode == "immediate"} == {
        "budget_threshold", "job_failed", "contract_expiring", "contract_expired", "review_requested"}
    assert {k for k, v in notifications.KINDS.items() if v.default_mode == "none"} == {
        "always_wrong", "citation_drop", "report_opened", "report_resolved", "judge_disagreement", "review_done"}


def test_echeance_du_recapitulatif(monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.delenv("GEOEVAL_DIGEST_TIME", raising=False)
    # 2026-10-03 : heure d'été, Paris = UTC+2 → 07:45 Paris = 05:45 UTC.
    before = datetime(2026, 10, 3, 5, 0, tzinfo=timezone.utc)
    after = datetime(2026, 10, 3, 6, 0, tzinfo=timezone.utc)
    assert notifications.digest_cutoff(after) == datetime(2026, 10, 3, 5, 45, tzinfo=timezone.utc)
    assert notifications.digest_cutoff(before) == datetime(2026, 10, 2, 5, 45, tzinfo=timezone.utc), "veille"
    winter = datetime(2026, 12, 3, 12, 0, tzinfo=timezone.utc)
    assert notifications.digest_cutoff(winter) == datetime(2026, 12, 3, 6, 45, tzinfo=timezone.utc), "heure d'hiver"
    monkeypatch.setenv("GEOEVAL_DIGEST_TIME", "18:30")
    assert notifications.digest_cutoff(after) == datetime(2026, 10, 2, 16, 30, tzinfo=timezone.utc)
    monkeypatch.setenv("GEOEVAL_DIGEST_TIME", "n'importe quoi")
    assert notifications.digest_cutoff(after) == datetime(2026, 10, 3, 5, 45, tzinfo=timezone.utc), "repli 07:45"


def test_calcul_de_la_chute_des_citations():
    pts = Decimal("20")
    assert detectors.citation_drop(0.5, [0.8, 0.8, 0.6], pts) == pytest.approx((0.7333, 23.33), abs=1e-2)
    assert detectors.citation_drop(0.6, [0.8, 0.8, 0.6], pts) is None, "13 points : sous le seuil"
    assert detectors.citation_drop(0.0, [0.8, 0.8], pts) is None, "3 runs de référence exigés"
    assert detectors.citation_drop(0.0, [None, 0.8, 0.8], pts) is None, "les runs sans citation ne comptent pas"
    assert detectors.citation_drop(None, [0.8, 0.8, 0.8], pts) is None
    assert detectors.citation_drop(0.5, [0.7, 0.7, 0.7], pts) is not None, "pile au seuil malgré les flottants"
    assert detectors.citation_drop(0.6, [0.8, 0.8, 0.8, 0.0], pts) == pytest.approx((0.8, 20.0)), "3 plus récents, ≥ seuil"


def test_defaut_ecart_citations(monkeypatch):
    monkeypatch.delenv("GEOEVAL_CITATION_DROP_POINTS", raising=False)
    assert detectors.citation_drop_default() == Decimal("20")
    monkeypatch.setenv("GEOEVAL_CITATION_DROP_POINTS", "12,5")
    assert detectors.citation_drop_default() == Decimal("12.5")
    monkeypatch.setenv("GEOEVAL_CITATION_DROP_POINTS", "500")
    assert detectors.citation_drop_default() == Decimal("100")
    monkeypatch.setenv("GEOEVAL_CITATION_DROP_POINTS", "abc")
    assert detectors.citation_drop_default() == Decimal("20")


def test_defauts_calibration(monkeypatch):
    monkeypatch.delenv("GEOEVAL_CALIBRATION_MIN_RHO", raising=False)
    monkeypatch.delenv("GEOEVAL_CALIBRATION_MIN_PAIRS", raising=False)
    assert detectors.calibration_defaults() == (Decimal("0.5"), 10)
    monkeypatch.setenv("GEOEVAL_CALIBRATION_MIN_RHO", "0,7")
    monkeypatch.setenv("GEOEVAL_CALIBRATION_MIN_PAIRS", "1")
    assert detectors.calibration_defaults() == (Decimal("0.7"), 3), "au moins 3 paires"
    monkeypatch.setenv("GEOEVAL_CALIBRATION_MIN_RHO", "2")
    assert detectors.calibration_defaults()[0] == Decimal("1")
    monkeypatch.setenv("GEOEVAL_CALIBRATION_MIN_RHO", "abc")
    assert detectors.calibration_defaults()[0] == Decimal("0.5")


def test_portees_en_desaccord():
    scopes = [(None, "tous thèmes", {"n_pairs": 12, "response_spearman": 0.3}),
              (1, "Fiscalité", {"n_pairs": 4, "response_spearman": -0.5}),       # trop peu de paires
              (2, "Santé", {"n_pairs": 10, "response_spearman": 0.8}),
              (3, "Emploi", {"n_pairs": 10, "response_spearman": None})]
    assert detectors.disagreements(scopes, Decimal("0.5"), 10) == [(None, "tous thèmes", 0.3, 12)]
    assert [s[0] for s in detectors.disagreements(scopes, Decimal("0.9"), 4)] == [None, 1, 2]
