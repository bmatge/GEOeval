"""Service de lancement (geoeval.web.launching) — les règles vivent ici, pas
dans les contrôleurs (ADR-088 §2.3, lot 1.3a) : liste blanche, validation
dans le périmètre, devis + plafond budgétaire, mise en file."""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select, text

from geoeval.db.models import Job, Model, Perimeter, ScheduledRun
from geoeval.web import budget, launching, org_models, pricing, services
from tests.conftest import TEST_ORG_SLUG

pytestmark = pytest.mark.integration


@pytest.fixture()
def corpus(db_session, test_org):
    """Un périmètre avec deux questions prêtes, un périmètre vide, les modèles du seed."""
    org_id = test_org["id"]
    db_session.execute(text("DELETE FROM jobs"))
    db_session.execute(text("DELETE FROM scheduled_runs WHERE organization_id = :o"), {"o": org_id})
    db_session.execute(text("DELETE FROM test_ground_truth WHERE test_id IN (SELECT test_id FROM tests WHERE organization_id = :o)"), {"o": org_id})
    db_session.execute(text("DELETE FROM tests WHERE organization_id = :o"), {"o": org_id})
    db_session.execute(text("DELETE FROM perimeters WHERE organization_id = :o"), {"o": org_id})
    db_session.execute(text("DELETE FROM budgets WHERE organization_id = :o"), {"o": org_id})
    org_models.clear_allowlist(db_session, org_id)
    db_session.commit()
    peri = Perimeter(organization_id=org_id, name="Site A", slug="site-a", kind="site")
    empty = Perimeter(organization_id=org_id, name="Site vide", slug="site-vide", kind="site")
    db_session.add_all([peri, empty])
    db_session.commit()
    t1 = services.create_test(db_session, org_id, perimeter_id=peri.id, prompt="Q1 ?", expected_answer="R1",
                              response_quality_prompt_id=None, citation_quality_prompt_id=None)
    t2 = services.create_test(db_session, org_id, perimeter_id=peri.id, prompt="Q2 ?", expected_answer="R2",
                              response_quality_prompt_id=None, citation_quality_prompt_id=None)
    tested = db_session.execute(select(Model).where(Model.model_name == "openrouter", Model.is_active.is_(True)).order_by(Model.model_id)).scalars().first()
    judge = db_session.execute(select(Model).where(Model.model_name == "albert", Model.is_judge.is_(True)).order_by(Model.model_id)).scalars().first()
    assert tested is not None and judge is not None
    return dict(org_id=org_id, peri=peri, empty=empty, tests=[t1.test_id, t2.test_id],
                tested=tested, judge=judge)


def _args(c, **over):
    base = dict(perimeter_id=c["peri"].id, tested_models=[c["tested"].model_version],
                judge_models=[c["judge"].model_version], repeats=2, test_ids=list(c["tests"]),
                role=None, is_platform_admin=True)
    base.update(over)
    return base


# ---------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------
def test_validation_nominale_toutes_les_questions(db_session, corpus):
    p = launching.validate_selection(db_session, corpus["org_id"], **_args(corpus))
    assert p["tested_models"] == [corpus["tested"].model_version]
    assert p["judges"] == [{"model": corpus["judge"].model_version, "repeats": 2}]
    assert p["test_ids"] is None, "toutes les questions du périmètre ⇒ pas de sous-sélection"


def test_validation_sous_selection(db_session, corpus):
    p = launching.validate_selection(db_session, corpus["org_id"], **_args(corpus, test_ids=[corpus["tests"][0]]))
    assert p["test_ids"] == [corpus["tests"][0]]


@pytest.mark.parametrize("over,motif", [
    (dict(tested_models=[]), "au moins une IA"),
    (dict(judge_models=[]), "au moins une IA"),
    (dict(perimeter_id=999_999), "Périmètre invalide"),
    (dict(test_ids=[]), "au moins une question"),
    (dict(test_ids=[999_999]), "hors du périmètre"),
])
def test_validation_refus(db_session, corpus, over, motif):
    with pytest.raises(launching.LaunchError) as ei:
        launching.validate_selection(db_session, corpus["org_id"], **_args(corpus, **over))
    assert ei.value.kind == "validation" and ei.value.status == 400 and motif in ei.value.detail


def test_validation_perimetre_sans_question(db_session, corpus):
    with pytest.raises(launching.LaunchError, match="Aucune question"):
        launching.validate_selection(db_session, corpus["org_id"], **_args(corpus, perimeter_id=corpus["empty"].id))


def test_repeats_minimum_1(db_session, corpus):
    p = launching.validate_selection(db_session, corpus["org_id"], **_args(corpus, repeats=0))
    assert p["judges"][0]["repeats"] == 1


# ---------------------------------------------------------------------
# Liste blanche (EPIC-001 S4.2) — garde-fou serveur
# ---------------------------------------------------------------------
def test_allowlist_refuse_editor_mais_pas_org_admin(db_session, corpus):
    org_models.replace_allowlist(db_session, corpus["org_id"], {corpus["judge"].model_id})
    try:
        with pytest.raises(launching.LaunchError) as ei:
            launching.validate_selection(db_session, corpus["org_id"], **_args(corpus, role="editor", is_platform_admin=False))
        assert ei.value.kind == "forbidden_models" and ei.value.status == 403
        assert ei.value.extra["forbidden"] == [corpus["tested"].model_version]
        # org_admin et admin plateforme échappent à la liste blanche.
        launching.validate_selection(db_session, corpus["org_id"], **_args(corpus, role="org_admin", is_platform_admin=False))
        launching.validate_selection(db_session, corpus["org_id"], **_args(corpus, role="viewer", is_platform_admin=True))
        allowed = launching.allowed_models(db_session, corpus["org_id"], role="editor", is_platform_admin=False)
        assert [m.model_id for m in allowed] == [corpus["judge"].model_id]
        assert launching.allowed_model_versions(db_session, corpus["org_id"], role="org_admin", is_platform_admin=False) is None
    finally:
        org_models.clear_allowlist(db_session, corpus["org_id"])


# ---------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------
@pytest.fixture()
def priced(db_session, corpus):
    pricing.set_pricing(db_session, model_id=corpus["tested"].model_id, input_eur_per_1m=Decimal("10"), output_eur_per_1m=Decimal("30"))
    pricing.set_pricing(db_session, model_id=corpus["judge"].model_id, input_eur_per_1m=Decimal("5"), output_eur_per_1m=Decimal("15"))
    return corpus


def test_budget_sans_plafond_laisse_passer(db_session, priced):
    params = launching.validate_selection(db_session, priced["org_id"], **_args(priced))
    est = launching.estimate_and_check_budget(db_session, priced["org_id"], params)
    assert est["total_eur"] > 0


def test_budget_plafond_zero_refuse(db_session, priced):
    budget.set_cap(db_session, org_id=priced["org_id"], cap_eur=Decimal("0"), updated_by=None)
    params = launching.validate_selection(db_session, priced["org_id"], **_args(priced))
    with pytest.raises(launching.LaunchError) as ei:
        launching.estimate_and_check_budget(db_session, priced["org_id"], params)
    assert ei.value.kind == "budget" and ei.value.status == 402
    assert "dépassé" in ei.value.detail and Decimal(ei.value.extra["estimate_eur"]) > 0


def test_budget_plafond_journalier(db_session, priced):
    budget.set_cap(db_session, org_id=priced["org_id"], cap_eur=Decimal("1000"), updated_by=None, daily_cap_eur=Decimal("0"))
    params = launching.validate_selection(db_session, priced["org_id"], **_args(priced))
    with pytest.raises(launching.LaunchError, match="journalier"):
        launching.estimate_and_check_budget(db_session, priced["org_id"], params)


# ---------------------------------------------------------------------
# Mise en file
# ---------------------------------------------------------------------
def test_launch_run_met_en_file(db_session, priced):
    job = launching.launch_run(db_session, priced["org_id"], note="essai", **_args(priced))
    assert job.status == "queued" and job.organization_id == priced["org_id"]
    assert job.params["tested_models"] == [priced["tested"].model_version]
    assert job.params["perimeter_id"] == priced["peri"].id and job.params["note"] == "essai"
    assert job.params["test_ids"] is None and "estimate" not in job.params


def test_launch_run_refuse_si_budget(db_session, priced):
    budget.set_cap(db_session, org_id=priced["org_id"], cap_eur=Decimal("0"), updated_by=None)
    with pytest.raises(launching.LaunchError, match="dépassé"):
        launching.launch_run(db_session, priced["org_id"], note=None, **_args(priced))
    assert db_session.execute(select(Job).where(Job.organization_id == priced["org_id"])).scalars().first() is None


def test_run_schedule_now_applique_la_meme_regle_budget(db_session, priced):
    sr = ScheduledRun(organization_id=priced["org_id"], perimeter_id=priced["peri"].id, name="hebdo",
                      tested_models=[priced["tested"].model_version],
                      judges=[{"model": priced["judge"].model_version, "repeats": 1}], test_ids=None,
                      schedule_kind="daily", schedule_config={"time": "09:00"}, enabled=True)
    db_session.add(sr)
    db_session.commit()
    job = launching.run_schedule_now(db_session, priced["org_id"], sr)
    assert job.status == "queued" and job.params["note"].endswith("(manuel)")

    budget.set_cap(db_session, org_id=priced["org_id"], cap_eur=Decimal("0"), updated_by=None)
    with pytest.raises(launching.LaunchError) as ei:
        launching.run_schedule_now(db_session, priced["org_id"], sr)
    assert ei.value.kind == "budget"


# ---------------------------------------------------------------------
# Traduction HTTP par l'UI (le contrôleur ne porte plus la règle)
# ---------------------------------------------------------------------
def test_ui_launch_traduit_les_refus(client, db_session, priced):
    base = dict(perimeter_id=priced["peri"].id, judge_models=[priced["judge"].model_version],
                test_ids=[str(t) for t in priced["tests"]], repeats="1")
    r = client.post(f"/o/{TEST_ORG_SLUG}/launch", data={**base, "tested_models": []})
    assert r.status_code == 400 and "au moins une IA" in r.json()["detail"]

    budget.set_cap(db_session, org_id=priced["org_id"], cap_eur=Decimal("0"), updated_by=None)
    r = client.post(f"/o/{TEST_ORG_SLUG}/launch", data={**base, "tested_models": [priced["tested"].model_version]})
    assert r.status_code == 402 and "dépassé" in r.json()["detail"]

    budget.set_cap(db_session, org_id=priced["org_id"], cap_eur=Decimal("1000"), updated_by=None)
    r = client.post(f"/o/{TEST_ORG_SLUG}/launch", data={**base, "tested_models": [priced["tested"].model_version]})
    assert r.status_code == 303 and r.headers["location"].startswith(f"/o/{TEST_ORG_SLUG}/jobs/")
