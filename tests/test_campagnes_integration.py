"""Cycle de vie des questions et campagnes (E8) sur PostgreSQL."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from geoeval.core.load import load_tests
from geoeval.db.models import AuditLog, Campaign, Job, Model, Organization, Perimeter, RunEvaluation, RunRow
from geoeval.web import api_tokens, budget, campaigns, launching, pools, services, tenancy
from geoeval.worker import jobs

pytestmark = pytest.mark.integration

PREFIX = "e8-"


@pytest.fixture()
def clean(db_session):
    def _purge():
        ids = [o.id for o in db_session.execute(select(Organization).where(Organization.slug.like(PREFIX + "%"))).scalars()]
        p = {"ids": ids or [-1]}
        tests_of = "SELECT test_id FROM tests WHERE organization_id = ANY(:ids)"
        camps = "SELECT id FROM campaigns WHERE owner_org_id = ANY(:ids)"
        runs_of = f"SELECT run_id FROM runs WHERE organization_id = ANY(:ids) OR campaign_id IN ({camps})"
        db_session.execute(text(f"DELETE FROM run_evaluations WHERE run_id IN ({runs_of}) OR test_id IN ({tests_of})"), p)
        db_session.execute(text(f"DELETE FROM run_results WHERE run_id IN ({runs_of}) OR test_id IN ({tests_of})"), p)
        db_session.execute(text(f"DELETE FROM usage WHERE run_id IN ({runs_of}) OR organization_id = ANY(:ids)"), p)
        db_session.execute(text(f"DELETE FROM runs WHERE run_id IN ({runs_of})"), p)
        db_session.execute(text("DELETE FROM job_logs WHERE job_id IN (SELECT id FROM jobs WHERE organization_id = ANY(:ids))"), p)
        db_session.execute(text(f"DELETE FROM campaigns WHERE id IN ({camps})"), p)
        db_session.execute(text("DELETE FROM question_pools WHERE owner_org_id = ANY(:ids)"), p)
        db_session.execute(text(f"DELETE FROM test_ground_truth WHERE test_id IN ({tests_of})"), p)
        db_session.execute(text(f"DELETE FROM test_themes WHERE test_id IN ({tests_of})"), p)
        for tbl, col in (("jobs", "organization_id"), ("notifications", "organization_id"), ("budget_alerts", "organization_id"),
                         ("budgets", "organization_id"), ("tests", "organization_id"), ("perimeters", "organization_id"),
                         ("api_tokens", "organization_id"), ("memberships", "org_id"), ("audit_log", "org_id")):
            db_session.execute(text(f"DELETE FROM {tbl} WHERE {col} = ANY(:ids)"), p)
        db_session.execute(text("UPDATE organizations SET parent_id = NULL WHERE id = ANY(:ids)"), p)
        db_session.execute(text("DELETE FROM organizations WHERE id = ANY(:ids)"), p)
        db_session.commit()
    _purge()
    yield
    _purge()


def _q(db, org, peri, prompt, status="published", expected="R"):
    return services.create_test(db, org.id, perimeter_id=peri.id, prompt=prompt, expected_answer=expected,
                                response_quality_prompt_id=None, citation_quality_prompt_id=None, status=status)


@pytest.fixture()
def monde(db_session, clean):
    m = tenancy.create_org(db_session, name="Ministère E8", slug=PREFIX + "min")
    d1 = tenancy.create_org(db_session, name="Direction A E8", slug=PREFIX + "dira", parent=m)
    d2 = tenancy.create_org(db_session, name="Direction B E8", slug=PREFIX + "dirb", parent=m)
    other = tenancy.create_org(db_session, name="Autre E8", slug=PREFIX + "autre")
    peri = Perimeter(organization_id=m.id, name="Site E8", slug="site-e8", kind="site")
    db_session.add(peri)
    db_session.commit()
    q = [_q(db_session, m, peri, f"Question E8 {i} ?") for i in range(3)]
    draft = _q(db_session, m, peri, "Brouillon E8 ?", status="draft")
    pool = pools.create(db_session, m, name="Socle E8", visibility="descendants")
    pools.add_tests(db_session, pool, [t.test_id for t in q])
    tested = db_session.execute(select(Model).where(Model.model_name == "openrouter", Model.is_active.is_(True))
                                .order_by(Model.model_id)).scalars().first()
    judge = db_session.execute(select(Model).where(Model.model_name == "albert", Model.is_judge.is_(True))
                               .order_by(Model.model_id)).scalars().first()
    return dict(m=m, d1=d1, d2=d2, other=other, peri=peri, q=q, draft=draft, pool=pool, tested=tested, judge=judge)


# ---------------------------------------------------------------------
# Cycle de vie des questions
# ---------------------------------------------------------------------
def test_cycle_brouillon_publiee_retiree(db_session, monde):
    m, draft, q = monde["m"], monde["draft"], monde["q"]
    active_ids = {t.test_id for t in load_tests(db_session, organization_id=m.id)}
    assert draft.test_id not in active_ids and {t.test_id for t in q} <= active_ids, "un brouillon n'entre pas dans un run"
    with pytest.raises(pools.PoolError) as ei:
        pools.add_tests(db_session, monde["pool"], [draft.test_id])
    assert ei.value.status == 400 and "brouillon" in ei.value.detail
    with pytest.raises(ValueError):
        services.reactivate_test(db_session, m.id, draft.test_id)
    services.publish_test(db_session, m.id, draft.test_id)
    assert draft.status == "published" and draft.test_id in {t.test_id for t in load_tests(db_session, organization_id=m.id)}
    with pytest.raises(ValueError):
        services.publish_test(db_session, m.id, draft.test_id)
    services.deactivate_test(db_session, m.id, draft.test_id)
    db_session.refresh(draft)
    assert draft.status == "retired" and draft.validity_end_at is not None
    services.reactivate_test(db_session, m.id, draft.test_id)
    db_session.refresh(draft)
    assert draft.status == "published" and draft.validity_end_at is None


def test_questions_effectives_ignorent_les_brouillons(db_session, monde):
    peri = monde["peri"]
    ids = {t.test_id for t in pools.effective_tests(db_session, peri)}
    assert monde["draft"].test_id not in ids and len(ids) == 3


# ---------------------------------------------------------------------
# Campagnes : brouillon, activation, protocole figé
# ---------------------------------------------------------------------
def _camp(db, w, **kw):
    args = dict(name="Campagne E8", source_pool_id=w["pool"].id, tested_models=[w["tested"].model_version],
                judges=[{"model": w["judge"].model_version, "repeats": 2}], schedule_kind="daily",
                schedule_config={"time": "06:00"}, participant_ids=[w["d1"].id, w["d2"].id])
    args.update(kw)
    return campaigns.create(db, w["m"], **args)


def test_creation_et_validation(db_session, monde):
    c = _camp(db_session, monde)
    assert c.status == "draft" and campaigns.participant_ids(db_session, c.id) == sorted([monde["d1"].id, monde["d2"].id])
    for bad in (dict(name=" "), dict(participant_ids=[monde["other"].id]), dict(tested_models=["inexistant"]),
                dict(judges=[{"model": monde["tested"].model_version if not monde["tested"].is_judge else "x"}])):
        with pytest.raises(campaigns.CampaignError):
            _camp(db_session, monde, **bad)
    secret = pools.create(db_session, monde["other"], name="Secret E8", visibility="private")
    with pytest.raises(campaigns.CampaignError) as ei:
        _camp(db_session, monde, source_pool_id=secret.id)
    assert ei.value.status == 404


def test_activation_fige_le_protocole(db_session, monde):
    m, q, pool = monde["m"], monde["q"], monde["pool"]
    c = _camp(db_session, monde)
    campaigns.activate(db_session, c)
    assert c.status == "active" and c.next_run_at is not None and c.activated_at is not None
    assert c.protocol["test_ids"] == sorted(t.test_id for t in q)
    assert c.protocol["judges"] == [{"model": monde["judge"].model_version, "repeats": 2}]
    # Le pool change : la campagne non.
    extra = _q(db_session, m, monde["peri"], "Question E8 ajoutée ?")
    pools.add_tests(db_session, pool, [extra.test_id])
    db_session.refresh(c)
    assert extra.test_id not in c.protocol["test_ids"]
    # Protocole figé, participants modifiables.
    with pytest.raises(campaigns.CampaignError) as ei:
        campaigns.update(db_session, c, tested_models=[])
    assert ei.value.status == 409
    campaigns.update(db_session, c, participant_ids=[monde["d1"].id], name="Campagne E8 bis")
    assert campaigns.participant_ids(db_session, c.id) == [monde["d1"].id] and c.name == "Campagne E8 bis"
    # Grille verrouillée tant que la campagne est active.
    other_grid = db_session.execute(text("SELECT max(prompt_id) FROM evaluation_prompts WHERE prompt_type_id = 1")).scalar_one()
    t = q[0]
    assert other_grid != t.response_quality_prompt_id, "la seed doit offrir au moins deux grilles de réponse"
    with pytest.raises(ValueError, match="grille"):
        services.update_test(db_session, m.id, t.test_id, prompt=t.prompt, expected_answer=t.expected_answer,
                             response_quality_prompt_id=other_grid, citation_quality_prompt_id=t.citation_quality_prompt_id)
    services.update_test(db_session, m.id, t.test_id, prompt="Libellé corrigé ?", expected_answer=t.expected_answer,
                         response_quality_prompt_id=t.response_quality_prompt_id,
                         citation_quality_prompt_id=t.citation_quality_prompt_id)
    with pytest.raises(campaigns.CampaignError):
        campaigns.activate(db_session, c)
    with pytest.raises(campaigns.CampaignError):
        campaigns.delete_draft(db_session, c)
    campaigns.close(db_session, c)
    assert c.status == "closed" and c.next_run_at is None and t.test_id not in campaigns.locked_test_ids(db_session)
    with pytest.raises(campaigns.CampaignError):
        campaigns.update(db_session, c, name="x")


@pytest.mark.parametrize("change,motif", [
    (dict(participant_ids=[]), "participant"),
    (dict(tested_models=[]), "IA évaluée"),
    (dict(schedule_kind="once", schedule_config={"at": "2020-01-01T08:00"}), "passée"),
])
def test_activation_refusee(db_session, monde, change, motif):
    c = _camp(db_session, monde, **change)
    with pytest.raises(campaigns.CampaignError) as ei:
        campaigns.activate(db_session, c)
    assert motif in ei.value.detail and c.status == "draft"


def test_activation_sans_question_publiee(db_session, monde):
    empty = pools.create(db_session, monde["m"], name="Vide E8")
    c = _camp(db_session, monde, source_pool_id=empty.id)
    with pytest.raises(campaigns.CampaignError, match="aucune question"):
        campaigns.activate(db_session, c)


# ---------------------------------------------------------------------
# Exécution
# ---------------------------------------------------------------------
@pytest.fixture()
def active(db_session, monde):
    c = _camp(db_session, monde)
    campaigns.activate(db_session, c)
    return c


def test_execution_par_participant_et_saut_tracé(db_session, monde, active, monkeypatch):
    from geoeval.web import pricing

    monkeypatch.setattr(pricing, "estimate_scan_cost", lambda *a, **k: {"total_eur": Decimal("1"), "by_model": [], "unpriced": []})
    budget.set_cap(db_session, org_id=monde["d2"].id, cap_eur=Decimal("0.5"), updated_by=None)
    ex = campaigns.execute(db_session, active)
    assert [o for o, _ in ex.queued] == [monde["d1"].id]
    assert [(o, k) for o, k, _ in ex.skipped] == [(monde["d2"].id, "budget")]
    job = db_session.get(Job, ex.queued[0][1])
    assert job.params["campaign_id"] == active.id and job.params["organization_id"] == monde["d1"].id
    assert job.params["test_ids"] == active.protocol["test_ids"] and job.params["perimeter_id"] is None
    parts = {o.id: p for p, o in campaigns.participants(db_session, active.id)}
    assert parts[monde["d1"].id].last_job_id == job.id and parts[monde["d2"].id].last_skip_reason
    assert db_session.execute(select(AuditLog).where(AuditLog.org_id == monde["d2"].id,
                                                     AuditLog.action == "skip_budget")).scalars().first()


def test_questions_du_participant_et_garde_fou(db_session, monde, active):
    q_ids = [t.test_id for t in monde["q"]]
    got = jobs.select_tests(db_session, organization_id=monde["d1"].id, perimeter_id=None, test_ids=None, campaign_id=active.id)
    assert [t.test_id for t in got] == sorted(q_ids), "les questions du ministère, exécutées par la direction"
    assert jobs.select_tests(db_session, organization_id=monde["other"].id, perimeter_id=None, test_ids=None,
                             campaign_id=active.id) == [], "non participant : rien"
    services.deactivate_test(db_session, monde["m"].id, q_ids[0])
    got = launching.tests_for_run(db_session, monde["d1"].id, perimeter_id=None, campaign_id=active.id)
    assert q_ids[0] not in {t.test_id for t in got}, "une question retirée sort des exécutions"


def test_planificateur_execute_les_campagnes_echues(db_session, monde, active, monkeypatch):
    from geoeval.web import pricing
    from geoeval.worker import scheduler

    monkeypatch.setattr(pricing, "estimate_scan_cost", lambda *a, **k: {"total_eur": Decimal("0"), "by_model": [], "unpriced": []})
    active.next_run_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.commit()
    scheduler.tick_if_leader(db_session)
    db_session.expire_all()
    c = db_session.get(Campaign, active.id)
    assert c.last_run_at is not None and c.next_run_at > datetime.now(timezone.utc)
    queued = db_session.execute(select(Job).where(Job.organization_id.in_([monde["d1"].id, monde["d2"].id]))).scalars().all()
    assert {j.params["campaign_id"] for j in queued} == {active.id} and len(queued) == 2


# ---------------------------------------------------------------------
# Résultats
# ---------------------------------------------------------------------
def _run(db, org, c, model, judge, test, score):
    r = RunRow(organization_id=org.id, tested_model_id=model.model_id, campaign_id=c.id)
    db.add(r)
    db.commit()
    db.add(RunEvaluation(run_id=r.run_id, test_id=test.test_id, judge_model_id=judge.model_id, judge_run_index=1,
                         response_quality_score=score, citation_quality_score=score - 1))
    db.commit()
    return r.run_id


def test_resultats_comparatifs(db_session, monde, active):
    d1, d2, tested, judge, t = monde["d1"], monde["d2"], monde["tested"], monde["judge"], monde["q"][0]
    _run(db_session, d1, active, tested, judge, t, 6)
    last = _run(db_session, d1, active, tested, judge, t, 8)
    _run(db_session, d2, active, tested, judge, t, 4)
    rows = campaigns.results(db_session, active)
    by_org = {r.org.id: r for r in rows}
    assert by_org[d1.id].n_runs == 2 and by_org[d1.id].last_run_id == last
    assert (by_org[d1.id].last_response, by_org[d1.id].delta_response, by_org[d1.id].delta_citation) == (8.0, 2.0, 2.0)
    assert by_org[d2.id].delta_response is None
    assert [r.org.id for r in campaigns.results(db_session, active, only_org_id=d2.id)] == [d2.id]


# ---------------------------------------------------------------------
# UI et API
# ---------------------------------------------------------------------
def test_ui_campagnes(client, db_session, monde):
    m, d1, pool = monde["m"], monde["d1"], monde["pool"]
    r = client.post(f"/o/{m.slug}/campaigns/new", data={
        "name": "Campagne UI E8", "source_pool_id": pool.id, "tested_models": [monde["tested"].model_version],
        "judge_models": [monde["judge"].model_version], "repeats": 1, "participant_ids": [d1.id],
        "schedule_kind": "weekly", "weekly_weekday": 2, "weekly_time": "07:30",
    })
    assert r.status_code == 303, r.text
    cid = int(r.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/o/{m.slug}/campaigns/{cid}")
    assert page.status_code == 200 and "Campagne UI E8" in page.text and "3 question(s) publiée(s)" in page.text
    assert client.get(f"/o/{m.slug}/campaigns/{cid}/edit").status_code == 200
    assert client.post(f"/o/{m.slug}/campaigns/{cid}/activate").status_code == 303
    assert "figé" in client.get(f"/o/{m.slug}/campaigns/{cid}").text
    assert "Campagne UI E8" in client.get(f"/o/{d1.slug}/campaigns").text, "visible du participant"
    assert client.get(f"/o/{d1.slug}/campaigns/{cid}").status_code == 200
    assert client.get(f"/o/{monde['other'].slug}/campaigns/{cid}").status_code == 404
    assert client.post(f"/o/{m.slug}/campaigns/{cid}/close").status_code == 303
    assert client.post(f"/o/{m.slug}/campaigns/{cid}/explode").status_code == 404
    # Cycle de vie dans l'UI : création en brouillon, publication.
    r = client.post(f"/o/{m.slug}/tests/new", data={"perimeter_id": monde["peri"].id, "prompt": "Brouillon UI E8 ?",
                                                    "expected_answer": "R", "draft": "true"})
    assert r.status_code == 303
    t = db_session.execute(text("SELECT test_id, status FROM tests WHERE prompt = 'Brouillon UI E8 ?'")).one()
    assert t.status == "draft" and "brouillon" in client.get(f"/o/{m.slug}/tests").text
    assert client.post(f"/o/{m.slug}/tests/{t.test_id}/publish").status_code == 303
    assert db_session.execute(text("SELECT status FROM tests WHERE test_id = :i"), {"i": t.test_id}).scalar_one() == "published"


def _bearer(db, org, role):
    _, plain = api_tokens.create(db, org_id=org.id, name=f"tok-{role}", role=role, created_by=None)
    return {"Authorization": f"Bearer {plain}"}


def test_api_campagnes_et_cycle_de_vie(client, db_session, monde, monkeypatch):
    from geoeval.web import pricing

    monkeypatch.setattr(pricing, "estimate_scan_cost", lambda *a, **k: {"total_eur": Decimal("0"), "by_model": [], "unpriced": []})
    m, d1, d2 = monde["m"], monde["d1"], monde["d2"]
    h_admin, h_editor, h_d1 = _bearer(db_session, m, "org_admin"), _bearer(db_session, m, "editor"), _bearer(db_session, d1, "viewer")
    base = f"/api/v1/orgs/{m.slug}/campaigns"
    body = {"name": "Campagne API E8", "source_pool_id": monde["pool"].id, "tested_models": [monde["tested"].model_version],
            "judge_models": [monde["judge"].model_version], "participant_ids": [d1.id, d2.id],
            "schedule_kind": "every_n_hours", "hours": 24}
    assert client.post(base, json=body, headers=h_editor).status_code == 403
    r = client.post(base, json=body, headers=h_admin)
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    assert r.json()["status"] == "draft" and len(r.json()["participants"]) == 2
    assert client.patch(f"{base}/{cid}", json={"repeats": 3}, headers=h_admin).json()["judges"][0]["repeats"] == 3
    r = client.post(f"{base}/{cid}/activate", headers=h_admin)
    assert r.status_code == 200 and len(r.json()["protocol"]["test_ids"]) == 3
    assert client.patch(f"{base}/{cid}", json={"tested_models": []}, headers=h_admin).status_code == 409
    ex = client.post(f"{base}/{cid}/run-now", headers=h_admin).json()
    assert len(ex["queued"]) == 2 and ex["skipped"] == []
    # Vue participant : sa ligne seulement.
    seen = client.get(f"/api/v1/orgs/{d1.slug}/campaigns/{cid}", headers=h_d1).json()
    assert seen["owned"] is False and [p["org_slug"] for p in seen["participants"]] == [d1.slug]
    _run(db_session, d1, db_session.get(Campaign, cid), monde["tested"], monde["judge"], monde["q"][0], 7)
    _run(db_session, d2, db_session.get(Campaign, cid), monde["tested"], monde["judge"], monde["q"][0], 5)
    assert {r["org_slug"] for r in client.get(f"{base}/{cid}/results", headers=h_editor).json()} == {d1.slug, d2.slug}
    assert [r["org_slug"] for r in client.get(f"/api/v1/orgs/{d1.slug}/campaigns/{cid}/results", headers=h_d1).json()] == [d1.slug]
    assert client.post(f"{base}/{cid}/close", headers=h_admin).json()["status"] == "closed"
    assert client.delete(f"{base}/{cid}", headers=h_admin).status_code == 409
    # Cycle de vie via l'API.
    q = client.post(f"/api/v1/orgs/{m.slug}/questions", json={"perimeter_id": monde["peri"].id, "prompt": "API brouillon E8 ?",
                                                             "status": "draft"}, headers=h_editor).json()
    assert q["status"] == "draft" and q["is_active"] is False
    r = client.post(f"/api/v1/orgs/{m.slug}/questions/{q['test_id']}/publish", headers=h_editor)
    assert r.status_code == 200 and r.json()["status"] == "published"
    assert client.post(f"/api/v1/orgs/{m.slug}/questions/{q['test_id']}/publish", headers=h_editor).status_code == 409
