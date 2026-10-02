"""API v1 écriture (lot 1.3c) : périmètres, questions, vérité de référence, planifications."""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select, text

from geoeval.db.models import Model, Perimeter, ScheduledRun
from geoeval.db.models import TestGroundTruth as GroundTruthRow  # alias : pytest ne doit pas le collecter
from geoeval.web import api_tokens, budget, org_models, pricing, services
from tests.conftest import TEST_ORG_SLUG

pytestmark = pytest.mark.integration

API = "/api/v1"
ORG = f"{API}/orgs/{TEST_ORG_SLUG}"


@pytest.fixture()
def corpus(db_session, test_org):
    org_id = test_org["id"]
    for sql in ("DELETE FROM job_logs", "DELETE FROM jobs"):
        db_session.execute(text(sql))
    db_session.execute(text("DELETE FROM test_ground_truth WHERE test_id IN (SELECT test_id FROM tests WHERE organization_id = :o)"), {"o": org_id})
    for tbl in ("scheduled_runs", "tests", "perimeters", "budgets", "api_tokens"):
        db_session.execute(text(f"DELETE FROM {tbl} WHERE organization_id = :o"), {"o": org_id})
    org_models.clear_allowlist(db_session, org_id)
    db_session.commit()
    peri = Perimeter(organization_id=org_id, name="Site W", slug="site-w", kind="site")
    db_session.add(peri)
    db_session.commit()
    t1 = services.create_test(db_session, org_id, perimeter_id=peri.id, prompt="Q1 ?", expected_answer="R1",
                              response_quality_prompt_id=None, citation_quality_prompt_id=None)
    tested = db_session.execute(select(Model).where(Model.model_name == "openrouter", Model.is_active.is_(True)).order_by(Model.model_id)).scalars().first()
    judge = db_session.execute(select(Model).where(Model.model_name == "albert", Model.is_judge.is_(True)).order_by(Model.model_id)).scalars().first()
    pricing.set_pricing(db_session, model_id=tested.model_id, input_eur_per_1m=Decimal("10"), output_eur_per_1m=Decimal("30"))
    pricing.set_pricing(db_session, model_id=judge.model_id, input_eur_per_1m=Decimal("5"), output_eur_per_1m=Decimal("15"))
    _, editor_plain = api_tokens.create(db_session, org_id=org_id, name="editor", role="editor", created_by=None)
    _, viewer_plain = api_tokens.create(db_session, org_id=org_id, name="viewer", role="viewer", created_by=None)
    return dict(org_id=org_id, peri=peri, test_id=t1.test_id, tested=tested, judge=judge,
                editor={"Authorization": f"Bearer {editor_plain}"}, viewer={"Authorization": f"Bearer {viewer_plain}"})


# ---------------------------------------------------------------------
# Périmètres
# ---------------------------------------------------------------------
def test_perimetres_cycle(anonymous_client, db_session, corpus):
    c, h = anonymous_client, corpus["editor"]
    r = c.post(f"{ORG}/perimeters", json={"name": "Nouveau site", "slug": "nouveau-site", "home_url": "https://a.gouv.fr"}, headers=h)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert r.json()["n_questions"] == 0 and r.json()["slug"] == "nouveau-site"

    r = c.post(f"{ORG}/perimeters", json={"name": "Doublon", "slug": "nouveau-site"}, headers=h)
    assert r.status_code == 409 and r.json()["title"] == "Conflit"
    assert c.post(f"{ORG}/perimeters", json={"name": "x", "slug": "Majuscule"}, headers=h).status_code == 422

    r = c.patch(f"{ORG}/perimeters/{pid}", json={"name": "Site renommé"}, headers=h)
    assert r.status_code == 200 and r.json()["name"] == "Site renommé" and r.json()["home_url"] == "https://a.gouv.fr"

    # Périmètre non vide : 409 ; vide : 204.
    assert c.delete(f"{ORG}/perimeters/{corpus['peri'].id}", headers=h).status_code == 409
    assert c.delete(f"{ORG}/perimeters/{pid}", headers=h).status_code == 204
    assert c.get(f"{ORG}/perimeters/{pid}", headers=h).status_code == 404
    assert c.delete(f"{ORG}/perimeters/999999", headers=h).status_code == 404


def test_perimetres_ecriture_refusee_au_viewer(anonymous_client, corpus):
    r = anonymous_client.post(f"{ORG}/perimeters", json={"name": "x", "slug": "x-y"}, headers=corpus["viewer"])
    assert r.status_code == 403


# ---------------------------------------------------------------------
# Questions et vérité de référence
# ---------------------------------------------------------------------
def test_questions_cycle(anonymous_client, db_session, corpus):
    c, h = anonymous_client, corpus["editor"]
    r = c.post(f"{ORG}/questions", json={"perimeter_id": corpus["peri"].id, "prompt": "Nouvelle ?", "expected_answer": "Oui"}, headers=h)
    assert r.status_code == 201, r.text
    q = r.json()
    assert q["is_active"] is True and q["response_quality_prompt_id"] is not None, "grilles par défaut posées"
    tid = q["test_id"]

    assert c.post(f"{ORG}/questions", json={"perimeter_id": 999999, "prompt": "x"}, headers=h).status_code == 400

    r = c.patch(f"{ORG}/questions/{tid}", json={"prompt": "Nouvelle, modifiée ?"}, headers=h)
    assert r.status_code == 200 and r.json()["prompt"] == "Nouvelle, modifiée ?" and r.json()["expected_answer"] == "Oui"

    other = Perimeter(organization_id=corpus["org_id"], name="Autre", slug="autre-w", kind="site")
    db_session.add(other)
    db_session.commit()
    r = c.patch(f"{ORG}/questions/{tid}", json={"perimeter_id": other.id}, headers=h)
    assert r.status_code == 200 and r.json()["perimeter_id"] == other.id
    assert c.patch(f"{ORG}/questions/{tid}", json={"perimeter_id": 999999}, headers=h).status_code == 400

    r = c.post(f"{ORG}/questions/{tid}/deactivate", headers=h)
    assert r.status_code == 200 and r.json()["is_active"] is False and r.json()["validity_end_at"] is not None
    r = c.post(f"{ORG}/questions/{tid}/reactivate", headers=h)
    assert r.status_code == 200 and r.json()["is_active"] is True
    assert c.post(f"{ORG}/questions/999999/deactivate", headers=h).status_code == 404
    assert "delete" not in {m for p, ops in anonymous_client.get(f"{API}/openapi.json").json()["paths"].items() if p.startswith("/orgs/{org_slug}/questions") for m in ops}, "jamais de DELETE sur une question (ADR-076)"


def test_verite_de_reference_versionnee(anonymous_client, db_session, corpus):
    c, h, tid = anonymous_client, corpus["editor"], corpus["test_id"]
    assert c.get(f"{ORG}/questions/{tid}/ground-truth", headers=corpus["viewer"]).json() == []
    r = c.post(f"{ORG}/questions/{tid}/ground-truth", json={"reference_answer": "V1", "reference_urls": [" https://a.fr ", ""]}, headers=h)
    assert r.status_code == 201 and r.json()["version"] == 1 and r.json()["reference_urls"] == ["https://a.fr"]
    r = c.post(f"{ORG}/questions/{tid}/ground-truth", json={"reference_answer": "V2", "notes": "corrigé"}, headers=h)
    assert r.status_code == 201 and r.json()["version"] == 2
    versions = c.get(f"{ORG}/questions/{tid}/ground-truth", headers=corpus["viewer"]).json()
    assert [v["version"] for v in versions] == [2, 1]
    assert c.get(f"{ORG}/questions/{tid}", headers=corpus["viewer"]).json()["ground_truth"]["reference_answer"] == "V2"
    closed = db_session.execute(select(GroundTruthRow).where(GroundTruthRow.test_id == tid, GroundTruthRow.version == 1)).scalar_one()
    assert closed.valid_to is not None, "la version précédente est clôturée, jamais écrasée"
    assert c.post(f"{ORG}/questions/{tid}/ground-truth", json={"reference_answer": "x"}, headers=corpus["viewer"]).status_code == 403


# ---------------------------------------------------------------------
# Planifications
# ---------------------------------------------------------------------
def _sched(c, **over):
    body = dict(perimeter_id=c["peri"].id, name="Quotidien", tested_models=[c["tested"].model_version],
                judge_models=[c["judge"].model_version], repeats=1, test_ids=[c["test_id"]],
                schedule_kind="daily", time="09:00")
    body.update(over)
    return body


def test_planifications_cycle(anonymous_client, db_session, corpus):
    c, h = anonymous_client, corpus["editor"]
    r = c.post(f"{ORG}/schedules", json=_sched(corpus), headers=h)
    assert r.status_code == 201, r.text
    sr = r.json()
    assert sr["enabled"] is True and sr["next_run_at"] is not None and sr["description"] == "chaque jour à 09:00"
    assert sr["test_ids"] is None, "toutes les questions du périmètre ⇒ pas de sous-sélection"
    sid = sr["schedule_id"]

    r = c.patch(f"{ORG}/schedules/{sid}", json={"enabled": False}, headers=h)
    assert r.status_code == 200 and r.json()["enabled"] is False
    r = c.patch(f"{ORG}/schedules/{sid}", json={"enabled": True, "name": "Quotidien bis"}, headers=h)
    assert r.status_code == 200 and r.json()["enabled"] is True and r.json()["name"] == "Quotidien bis" and r.json()["next_run_at"]

    assert c.delete(f"{ORG}/schedules/{sid}", headers=h).status_code == 204
    assert c.get(f"{ORG}/schedules/{sid}", headers=h).status_code == 404
    assert c.delete(f"{ORG}/schedules/{sid}", headers=h).status_code == 404


@pytest.mark.parametrize("over,status,motif", [
    (dict(schedule_kind="once", at="2020-01-01T08:00", time=None), 400, "déjà passée"),
    (dict(schedule_kind="weekly", time=None), 400, "hebdomadaires"),
    (dict(schedule_kind="every_n_hours", hours=None, time=None), 400, "au moins 1 heure"),
    (dict(name="   "), 400, "obligatoire"),
    (dict(test_ids=[999999]), 400, "hors du périmètre"),
])
def test_planifications_refus_validation(anonymous_client, corpus, over, status, motif):
    r = anonymous_client.post(f"{ORG}/schedules", json=_sched(corpus, **over), headers=corpus["editor"])
    assert r.status_code == status, r.text
    assert r.json()["type"] == "/api/v1/problems/validation" and motif in r.json()["detail"]


def test_planification_budget_et_role(anonymous_client, db_session, corpus):
    assert anonymous_client.post(f"{ORG}/schedules", json=_sched(corpus), headers=corpus["viewer"]).status_code == 403
    budget.set_cap(db_session, org_id=corpus["org_id"], cap_eur=Decimal("0"), updated_by=None)
    r = anonymous_client.post(f"{ORG}/schedules", json=_sched(corpus), headers=corpus["editor"])
    assert r.status_code == 402 and r.json()["type"] == "/api/v1/problems/budget"


def test_planification_once_passee_non_reactivable(anonymous_client, db_session, corpus):
    sr = ScheduledRun(organization_id=corpus["org_id"], perimeter_id=corpus["peri"].id, name="vieux one-shot",
                      tested_models=["m"], judges=[], test_ids=None, schedule_kind="once",
                      schedule_config={"at": "2020-01-01T08:00"}, enabled=False, next_run_at=None)
    db_session.add(sr)
    db_session.commit()
    r = anonymous_client.patch(f"{ORG}/schedules/{sr.schedule_id}", json={"enabled": True}, headers=corpus["editor"])
    assert r.status_code == 400 and "réactiver" in r.json()["detail"]


def test_ui_planification_passe_par_le_service(client, corpus):
    """Régression : le formulaire HTML utilise désormais scheduling.*."""
    base = dict(perimeter_id=corpus["peri"].id, name="Depuis UI", tested_models=[corpus["tested"].model_version],
                judge_models=[corpus["judge"].model_version], repeats="1", test_ids=[str(corpus["test_id"])])
    r = client.post(f"/o/{TEST_ORG_SLUG}/schedules/new", data={**base, "schedule_kind": "daily", "daily_time": "10:30"})
    assert r.status_code == 303, r.text
    r = client.post(f"/o/{TEST_ORG_SLUG}/schedules/new", data={**base, "schedule_kind": "once", "once_at": "2020-01-01T08:00"})
    assert r.status_code == 400 and "déjà passée" in r.json()["detail"]
    r = client.post(f"/o/{TEST_ORG_SLUG}/schedules/new", data={**base, "schedule_kind": "weekly", "weekly_weekday": "2", "weekly_time": "08:15"})
    assert r.status_code == 303
