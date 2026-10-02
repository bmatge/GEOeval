"""API v1 (lot 1.3b) : jetons, rôles, problem+json, lecture publique, lancement."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from geoeval.db.models import ApiToken, Model, Perimeter, ScheduledRun
from geoeval.web import api_tokens, budget, org_models, pricing, services, tenancy
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
    peri = Perimeter(organization_id=org_id, name="Site API", slug="site-api", kind="site", home_url="https://exemple.gouv.fr")
    db_session.add(peri)
    db_session.commit()
    t1 = services.create_test(db_session, org_id, perimeter_id=peri.id, prompt="Q1 ?", expected_answer="R1",
                              response_quality_prompt_id=None, citation_quality_prompt_id=None)
    t2 = services.create_test(db_session, org_id, perimeter_id=peri.id, prompt="Q2 ?", expected_answer="R2",
                              response_quality_prompt_id=None, citation_quality_prompt_id=None)
    tested = db_session.execute(select(Model).where(Model.model_name == "openrouter", Model.is_active.is_(True)).order_by(Model.model_id)).scalars().first()
    judge = db_session.execute(select(Model).where(Model.model_name == "albert", Model.is_judge.is_(True)).order_by(Model.model_id)).scalars().first()
    pricing.set_pricing(db_session, model_id=tested.model_id, input_eur_per_1m=Decimal("10"), output_eur_per_1m=Decimal("30"))
    pricing.set_pricing(db_session, model_id=judge.model_id, input_eur_per_1m=Decimal("5"), output_eur_per_1m=Decimal("15"))
    return dict(org_id=org_id, peri=peri, tests=[t1.test_id, t2.test_id], tested=tested, judge=judge)


def _token(db_session, org_id, role, **kw):
    _, plain = api_tokens.create(db_session, org_id=org_id, name=f"test-{role}", role=role, created_by=None, **kw)
    return {"Authorization": f"Bearer {plain}"}


def _launch_body(c, **over):
    body = dict(perimeter_id=c["peri"].id, tested_models=[c["tested"].model_version],
                judge_models=[c["judge"].model_version], repeats=1, test_ids=c["tests"], note="via api")
    body.update(over)
    return body


# ---------------------------------------------------------------------
# Documentation, identité, erreurs
# ---------------------------------------------------------------------
def test_openapi_et_docs(anonymous_client):
    r = anonymous_client.get(f"{API}/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    assert "/orgs/{org_slug}/runs" in paths and "/orgs/{org_slug}/tokens" in paths and len(paths) >= 20
    assert anonymous_client.get(f"{API}/docs").status_code == 200


def test_me_session_et_token(client, db_session, corpus):
    r = client.get(f"{API}/me")
    assert r.status_code == 200 and r.json()["kind"] == "session" and r.json()["is_platform_admin"] is True
    r = client.get(f"{API}/me", headers=_token(db_session, corpus["org_id"], "editor"))
    body = r.json()
    assert body["kind"] == "token" and body["token"]["role"] == "editor" and body["organizations"] == [{"slug": TEST_ORG_SLUG, "role": "editor"}]


def test_me_anonyme_401_problem(anonymous_client):
    r = anonymous_client.get(f"{API}/me")
    assert r.status_code == 401 and r.headers["content-type"].startswith("application/problem+json")
    assert r.headers["www-authenticate"] == "Bearer"
    body = r.json()
    assert body["status"] == 401 and body["title"] == "Authentification requise" and body["instance"] == f"{API}/me"


def test_jeton_invalide_401(anonymous_client):
    r = anonymous_client.get(f"{API}/me", headers={"Authorization": "Bearer geoeval_xxxxxxxx_faux"})
    assert r.status_code == 401 and "invalide" in r.json()["detail"]


def test_org_inconnue_404_problem(client):
    r = client.get(f"{API}/orgs/nexiste-pas")
    assert r.status_code == 404 and r.json()["type"] == "about:blank" and r.json()["status"] == 404


def test_jeton_autre_org_404(client, db_session, corpus):
    other = tenancy.get_org_by_slug(db_session, "autre-org-api") or tenancy.create_org(db_session, name="Autre", slug="autre-org-api")
    headers = _token(db_session, other.id, "org_admin")
    assert client.get(f"{ORG}", headers=headers).status_code == 404
    assert client.get(f"{API}/orgs/autre-org-api", headers=headers).status_code == 200
    assert [o["slug"] for o in client.get(f"{API}/orgs", headers=headers).json()] == ["autre-org-api"]


# ---------------------------------------------------------------------
# Lecture publique vs membres
# ---------------------------------------------------------------------
def test_lecture_publique_sans_compte(anonymous_client, corpus):
    assert anonymous_client.get(f"{ORG}").status_code == 200
    r = anonymous_client.get(f"{ORG}/runs")
    assert r.status_code == 200 and set(r.json()) == {"items", "total", "limit", "offset"}
    assert anonymous_client.get(f"{ORG}/stats/summary").json()["n_runs"] == 0
    assert anonymous_client.get(f"{ORG}/stats/leaderboard").status_code == 200
    assert anonymous_client.get(f"{ORG}/stats/questions").status_code == 200
    assert anonymous_client.get(f"{ORG}/stats/evolution").status_code == 200
    assert anonymous_client.get(f"{ORG}/runs/999999").status_code == 404
    assert len(anonymous_client.get(f"{API}/orgs").json()) >= 1


def test_corpus_reserve_aux_membres(anonymous_client, db_session, corpus):
    for path in ("/questions", "/perimeters", "/models", "/schedules", "/jobs"):
        assert anonymous_client.get(f"{ORG}{path}").status_code == 401, path
    viewer = _token(db_session, corpus["org_id"], "viewer")
    r = anonymous_client.get(f"{ORG}/questions", headers=viewer)
    assert r.status_code == 200 and r.json()["total"] == 2 and r.json()["items"][0]["is_active"] is True
    r = anonymous_client.get(f"{ORG}/questions?limit=1&offset=1", headers=viewer)
    assert len(r.json()["items"]) == 1 and r.json()["total"] == 2
    r = anonymous_client.get(f"{ORG}/questions/{corpus['tests'][0]}", headers=viewer)
    assert r.status_code == 200 and r.json()["ground_truth"] is None and r.json()["prompt"] == "Q1 ?"
    r = anonymous_client.get(f"{ORG}/perimeters", headers=viewer)
    assert r.json()[0]["slug"] == "site-api" and r.json()[0]["n_questions"] == 2
    assert anonymous_client.get(f"{ORG}/perimeters/{corpus['peri'].id}", headers=viewer).status_code == 200
    assert anonymous_client.get(f"{ORG}/perimeters/999999", headers=viewer).status_code == 404


def test_models_filtre_par_liste_blanche_et_sans_secret(anonymous_client, db_session, corpus):
    viewer = _token(db_session, corpus["org_id"], "viewer")
    admin = _token(db_session, corpus["org_id"], "org_admin")
    org_models.replace_allowlist(db_session, corpus["org_id"], {corpus["judge"].model_id})
    try:
        seen = anonymous_client.get(f"{ORG}/models", headers=viewer).json()
        assert [m["model_id"] for m in seen] == [corpus["judge"].model_id]
        assert "api_key" not in seen[0] and "extra_headers" not in seen[0]
        assert len(anonymous_client.get(f"{ORG}/models", headers=admin).json()) > 1
    finally:
        org_models.clear_allowlist(db_session, corpus["org_id"])


# ---------------------------------------------------------------------
# Lancement
# ---------------------------------------------------------------------
def test_lancement_par_jeton_editor(anonymous_client, db_session, corpus):
    editor = _token(db_session, corpus["org_id"], "editor")
    r = anonymous_client.post(f"{ORG}/runs", json=_launch_body(corpus), headers=editor)
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["status"] == "queued" and job["params"]["note"] == "via api"
    r = anonymous_client.get(f"{ORG}/jobs/{job['id']}", headers=editor)
    assert r.status_code == 200 and r.json()["id"] == job["id"] and "log" in r.json()
    r = anonymous_client.get(f"{ORG}/jobs", headers=editor)
    assert r.json()["total"] >= 1 and r.json()["items"][0]["id"] == job["id"]
    audit_rows = db_session.execute(text("SELECT meta_json FROM audit_log WHERE action='launch' AND org_id=:o ORDER BY id DESC LIMIT 1"), {"o": corpus["org_id"]}).scalar_one()
    assert audit_rows["via"] == "api" and audit_rows["kind"] == "token"


def test_lancement_refuse_au_viewer(anonymous_client, db_session, corpus):
    r = anonymous_client.post(f"{ORG}/runs", json=_launch_body(corpus), headers=_token(db_session, corpus["org_id"], "viewer"))
    assert r.status_code == 403 and r.json()["title"] == "Accès refusé"


def test_lancement_hors_liste_blanche_problem_type(anonymous_client, db_session, corpus):
    org_models.replace_allowlist(db_session, corpus["org_id"], {corpus["judge"].model_id})
    try:
        r = anonymous_client.post(f"{ORG}/runs", json=_launch_body(corpus), headers=_token(db_session, corpus["org_id"], "editor"))
        assert r.status_code == 403
        body = r.json()
        assert body["type"] == "/api/v1/problems/forbidden-models" and body["forbidden"] == [corpus["tested"].model_version]
    finally:
        org_models.clear_allowlist(db_session, corpus["org_id"])


def test_lancement_budget_402_problem(anonymous_client, db_session, corpus):
    budget.set_cap(db_session, org_id=corpus["org_id"], cap_eur=Decimal("0"), updated_by=None)
    r = anonymous_client.post(f"{ORG}/runs", json=_launch_body(corpus), headers=_token(db_session, corpus["org_id"], "editor"))
    assert r.status_code == 402
    body = r.json()
    assert body["type"] == "/api/v1/problems/budget" and body["title"] == "Plafond budgétaire atteint"
    assert Decimal(body["estimate_eur"]) > 0 and "dépassé" in body["detail"]


def test_lancement_corps_invalide_422_problem(anonymous_client, db_session, corpus):
    r = anonymous_client.post(f"{ORG}/runs", json=_launch_body(corpus, tested_models=[]), headers=_token(db_session, corpus["org_id"], "editor"))
    assert r.status_code == 422 and r.json()["status"] == 422 and r.json()["errors"]


def test_lancement_selection_invalide_400(anonymous_client, db_session, corpus):
    r = anonymous_client.post(f"{ORG}/runs", json=_launch_body(corpus, test_ids=[999999]), headers=_token(db_session, corpus["org_id"], "editor"))
    assert r.status_code == 400 and r.json()["type"] == "/api/v1/problems/validation"


def test_run_now_par_api(anonymous_client, db_session, corpus):
    sr = ScheduledRun(organization_id=corpus["org_id"], perimeter_id=corpus["peri"].id, name="api-sched",
                      tested_models=[corpus["tested"].model_version], judges=[{"model": corpus["judge"].model_version, "repeats": 1}],
                      test_ids=None, schedule_kind="daily", schedule_config={"time": "09:00"}, enabled=True)
    db_session.add(sr)
    db_session.commit()
    editor = _token(db_session, corpus["org_id"], "editor")
    r = anonymous_client.get(f"{ORG}/schedules", headers=editor)
    assert r.status_code == 200 and r.json()[0]["description"] == "chaque jour à 09:00"
    r = anonymous_client.post(f"{ORG}/schedules/{sr.schedule_id}/run-now", headers=editor)
    assert r.status_code == 202 and r.json()["params"]["note"].endswith("(manuel)")
    assert anonymous_client.post(f"{ORG}/schedules/999999/run-now", headers=editor).status_code == 404


# ---------------------------------------------------------------------
# Jetons : cycle de vie via l'API et via l'UI
# ---------------------------------------------------------------------
def test_jetons_api_cycle_de_vie(client, db_session, corpus):
    """Un seul client : le jeton Bearer prime sur la session, et un jeton révoqué
    est refusé même si une session existe (401, pas de repli silencieux)."""
    r = client.post(f"{ORG}/tokens", json={"name": "Jenkins", "role": "editor", "expires_in_days": 30})
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["token"].startswith("geoeval_") and created["role"] == "editor" and created["active"] is True
    listed = client.get(f"{ORG}/tokens").json()
    assert listed[0]["prefix"] == created["prefix"] and "token" not in listed[0]
    headers = {"Authorization": f"Bearer {created['token']}"}
    assert client.get(f"{API}/me", headers=headers).json()["kind"] == "token"
    assert client.get(f"{ORG}/questions", headers=headers).status_code == 200
    db_session.expire_all()
    assert db_session.get(ApiToken, created["id"]).last_used_at is not None

    assert client.delete(f"{ORG}/tokens/{created['id']}").status_code == 204
    assert client.get(f"{ORG}/questions", headers=headers).status_code == 401
    assert client.delete(f"{ORG}/tokens/999999").status_code == 404


def test_jetons_reserves_aux_org_admin(anonymous_client, db_session, corpus):
    editor = _token(db_session, corpus["org_id"], "editor")
    assert anonymous_client.get(f"{ORG}/tokens", headers=editor).status_code == 403
    assert anonymous_client.post(f"{ORG}/tokens", json={"name": "x"}, headers=editor).status_code == 403
    admin = _token(db_session, corpus["org_id"], "org_admin")
    r = anonymous_client.post(f"{ORG}/tokens", json={"name": "sous-jeton", "role": "viewer"}, headers=admin)
    assert r.status_code == 201


def test_jeton_expire_refuse(anonymous_client, db_session, corpus):
    token, plain = api_tokens.create(db_session, org_id=corpus["org_id"], name="court", role="viewer", created_by=None, expires_in_days=1)
    token.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.commit()
    assert anonymous_client.get(f"{ORG}/questions", headers={"Authorization": f"Bearer {plain}"}).status_code == 401


def test_jetons_depuis_les_parametres_ui(client, db_session, corpus):
    r = client.post(f"/o/{TEST_ORG_SLUG}/settings/tokens", data={"name": "Depuis UI", "token_role": "viewer", "expires_in_days": ""})
    assert r.status_code == 200 and "geoeval_" in r.text and "copiez-le maintenant" in r.text
    page = client.get(f"/o/{TEST_ORG_SLUG}/settings").text
    assert "Depuis UI" in page and "copiez-le maintenant" not in page
    tok = db_session.execute(select(ApiToken).where(ApiToken.name == "Depuis UI")).scalars().first()
    r = client.post(f"/o/{TEST_ORG_SLUG}/settings/tokens/{tok.id}/revoke")
    assert r.status_code == 303
    db_session.expire_all()
    assert db_session.get(ApiToken, tok.id).revoked_at is not None
    r = client.post(f"/o/{TEST_ORG_SLUG}/settings/tokens", data={"name": "   ", "token_role": "viewer", "expires_in_days": ""})
    assert r.status_code == 200 and "obligatoire" in r.text
