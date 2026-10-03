"""Règles pures d'E5 : couverture et validité des contrats, analyse des saisies,
cohérence de la migration avec le mapping des familles."""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from geoeval.core import llm_clients
from geoeval.db.models import LlmContract
from geoeval.web import contracts


def _c(**kw):
    base = dict(family="mistral", label="c", model_ids=[], valid_from=None, valid_to=None, cap_eur=None,
                is_active=True, hosting=None, sovereign=False)
    base.update(kw)
    return LlmContract(**base)


MISTRAL = SimpleNamespace(model_id=3, model_name="mistral", model_version="mistral-large", hosting=None, is_sovereign=False)
ALBERT = SimpleNamespace(model_id=7, model_name="albert", model_version="albert-large", hosting="eu", is_sovereign=True)


def test_couverture_famille_et_restriction():
    assert contracts.covers(_c(), MISTRAL)
    assert not contracts.covers(_c(), ALBERT), "autre famille"
    assert contracts.covers(_c(model_ids=[3, 4]), MISTRAL)
    assert not contracts.covers(_c(model_ids=[4]), MISTRAL)


@pytest.mark.parametrize("vf,vt,day,ok", [
    (None, None, date(2026, 1, 1), True),
    (date(2026, 1, 1), None, date(2025, 12, 31), False),
    (date(2026, 1, 1), date(2026, 12, 31), date(2026, 12, 31), True),   # bornes incluses
    (None, date(2026, 6, 30), date(2026, 7, 1), False),
])
def test_periode_de_validite(vf, vt, day, ok):
    assert contracts.in_window(_c(valid_from=vf, valid_to=vt), day) is ok


def test_chevauchements():
    a = _c(valid_from=date(2026, 1, 1), valid_to=date(2026, 6, 30))
    b = _c(valid_from=date(2026, 7, 1))
    assert not contracts._windows_overlap(a, b), "contrat successeur"
    assert contracts._windows_overlap(a, _c())
    assert contracts._scopes_overlap(_c(), _c())
    assert contracts._scopes_overlap(_c(model_ids=[1, 2]), _c(model_ids=[2]))
    assert not contracts._scopes_overlap(_c(model_ids=[1]), _c(model_ids=[2]))
    assert not contracts._scopes_overlap(_c(model_ids=[1]), _c()), "restreint + famille coexistent"


def test_etat_d_affichage():
    day = date(2026, 5, 1)
    assert contracts.status(_c(), Decimal("0"), day) == "active"
    assert contracts.status(_c(is_active=False), Decimal("0"), day) == "inactive"
    assert contracts.status(_c(valid_from=date(2026, 6, 1)), Decimal("0"), day) == "upcoming"
    assert contracts.status(_c(valid_to=date(2026, 4, 30)), Decimal("0"), day) == "expired"
    assert contracts.status(_c(cap_eur=Decimal("10")), Decimal("10"), day) == "exhausted"


def test_hebergement_et_souverainete_effectifs():
    assert contracts.effective_hosting(MISTRAL, None) is None
    assert contracts.effective_hosting(MISTRAL, _c(hosting="eu")) == "eu", "le contrat surcharge le modèle"
    assert contracts.effective_hosting(ALBERT, _c(family="albert")) == "eu"
    assert contracts.effective_sovereign(ALBERT, None)
    assert not contracts.effective_sovereign(MISTRAL, _c())
    assert contracts.effective_sovereign(MISTRAL, _c(sovereign=True))


def test_analyse_des_saisies():
    assert contracts.parse_headers("") is None
    assert contracts.parse_headers('{"X-A": "1"}') == {"X-A": "1"}
    for bad in ('{"X": 1}', "[1]", "{pas du json"):
        with pytest.raises(contracts.ContractError):
            contracts.parse_headers(bad)
    assert contracts.parse_cap("1 500,5".replace(" ", "")) == Decimal("1500.50")
    assert contracts.parse_cap("") is None
    with pytest.raises(contracts.ContractError):
        contracts.parse_cap("-3")
    with pytest.raises(contracts.ContractError):
        contracts.parse_cap("beaucoup")
    assert contracts.parse_day("2026-03-01") == date(2026, 3, 1)
    with pytest.raises(contracts.ContractError):
        contracts.parse_day("01/03/2026")


def test_resolution_sans_entite_ni_famille():
    assert contracts.billing_for(None, None, MISTRAL).billed_to == "platform"
    res = contracts.ContractResolution(contract=_c(), blocked="expiré")
    assert not res.usable and res.contract_id is None and res.billed_to == "platform", "un contrat bloqué n'est jamais imputé"


def test_familles_coherentes_avec_la_migration():
    """La conversion SQL des clés BYOK (révision 0005) doit suivre llm_clients._FAMILY_BY_NAME."""
    src = (Path(__file__).resolve().parents[1] / "geoeval/db/alembic/versions/0005_contrats_llm_routage.py").read_text()
    pairs = dict(re.findall(r"WHEN '([a-z\-]+)' THEN '([a-z]+)'", src))
    assert pairs == llm_clients._FAMILY_BY_NAME
    assert set(llm_clients._FAMILY_BY_NAME.values()) == set(contracts.FAMILIES)
