"""Explorateur de scores : lignes par (évaluation, question) sur l'entité et son sous-arbre."""
from __future__ import annotations

import pytest
from sqlalchemy import select, text

from geoeval.db.models import Model, Organization, Perimeter, RunEvaluation, RunResult, RunRow
from geoeval.web import services, tenancy

pytestmark = pytest.mark.integration

PREFIX = "expl-"


@pytest.fixture()
def clean(db_session):
    def _purge():
        ids = [o.id for o in db_session.execute(select(Organization).where(Organization.slug.like(PREFIX + "%"))).scalars()]
        p = {"ids": ids or [-1]}
        tests_of = "SELECT test_id FROM tests WHERE organization_id = ANY(:ids)"
        runs_of = "SELECT run_id FROM runs WHERE organization_id = ANY(:ids)"
        db_session.execute(text(f"DELETE FROM run_evaluations WHERE run_id IN ({runs_of}) OR test_id IN ({tests_of})"), p)
        db_session.execute(text(f"DELETE FROM run_results WHERE run_id IN ({runs_of}) OR test_id IN ({tests_of})"), p)
        db_session.execute(text(f"DELETE FROM runs WHERE run_id IN ({runs_of})"), p)
        db_session.execute(text(f"DELETE FROM test_ground_truth WHERE test_id IN ({tests_of})"), p)
        for tbl, col in (("tests", "organization_id"), ("perimeters", "organization_id"), ("memberships", "org_id"),
                         ("audit_log", "org_id")):
            db_session.execute(text(f"DELETE FROM {tbl} WHERE {col} = ANY(:ids)"), p)
        db_session.execute(text("UPDATE organizations SET parent_id = NULL WHERE id = ANY(:ids)"), p)
        db_session.execute(text("DELETE FROM organizations WHERE id = ANY(:ids)"), p)
        db_session.commit()
    _purge()
    yield
    _purge()


def _corpus(db, org, slug, n=2):
    peri = Perimeter(organization_id=org.id, name=f"Site {slug}", slug=slug, kind="site")
    db.add(peri)
    db.commit()
    qs = [services.create_test(db, org.id, perimeter_id=peri.id, prompt=f"Question {slug} {i}", expected_answer="Réponse",
                               response_quality_prompt_id=None, citation_quality_prompt_id=None) for i in range(n)]
    return peri, qs


def _run(db, org, peri, tests, tested, judges, score):
    r = RunRow(organization_id=org.id, perimeter_id=peri.id, tested_model_id=tested.model_id)
    db.add(r)
    db.commit()
    for t in tests:
        db.add(RunResult(run_id=r.run_id, test_id=t.test_id, raw_answer="…", raw_citations=[]))
    db.commit()
    if score is not None:
        for t in tests:
            for j in judges:
                db.add(RunEvaluation(run_id=r.run_id, test_id=t.test_id, judge_model_id=j.model_id, judge_run_index=1,
                                     response_quality_score=score, citation_quality_score=score - 1))
        db.commit()
    return r.run_id


@pytest.fixture()
def monde(db_session, clean):
    m = tenancy.create_org(db_session, name="Ministère Expl", slug=PREFIX + "min")
    s = tenancy.create_org(db_session, name="Service Expl", slug=PREFIX + "svc", parent=m)
    autre = tenancy.create_org(db_session, name="Autre Expl", slug=PREFIX + "autre")
    tested = db_session.execute(select(Model).where(Model.model_name == "openrouter").order_by(Model.model_id)).scalars().all()[:2]
    judges = db_session.execute(select(Model).where(Model.model_name == "albert").order_by(Model.model_id)).scalars().all()[:2]
    return dict(m=m, s=s, autre=autre, tested=tested, judges=judges)


def test_lignes_de_scores_sous_arbre_et_dernier_passage(db_session, monde):
    m, s, autre = monde["m"], monde["s"], monde["autre"]
    (ia1, ia2), judges = monde["tested"], monde["judges"]
    peri_m, q_m = _corpus(db_session, m, "min")
    peri_s, q_s = _corpus(db_session, s, "svc")
    peri_a, q_a = _corpus(db_session, autre, "autre")
    old = _run(db_session, s, peri_s, q_s, ia1, judges, 4)
    new = _run(db_session, s, peri_s, q_s, ia1, judges, 8)        # second passage de la même IA : il fait foi
    other = _run(db_session, s, peri_s, q_s, ia2, judges, 6)
    top = _run(db_session, m, peri_m, q_m, ia1, judges, 9)
    pending = _run(db_session, m, peri_m, q_m, ia2, judges, None)  # pas encore noté
    _run(db_session, autre, peri_a, q_a, ia1, judges, 2)
    rows = services.score_rows(db_session, m)
    assert {r["run_id"] for r in rows} == {new, other, top, pending}, "sous-arbre, dernier passage par (entité, périmètre, IA)"
    assert {r["entite"] for r in rows} == {"Ministère Expl", "Service Expl"}, "l'entité voisine n'apparaît pas"
    assert len(rows) == 8
    r_new = next(r for r in rows if r["run_id"] == new and r["question_id"] == q_s[0].test_id)
    assert (r_new["note_reponse"], r_new["note_citations"], r_new["notes"]) == (8.0, 7.0, 2), "moyenne des 2 notateurs"
    assert (r_new["perimetre"], r_new["ia"], r_new["evaluation"]) == ("Site svc", ia1.model_version, f"#{new}")
    assert r_new["lien"] == f"/o/{s.slug}/runs/{new}" and r_new["question"] == "Question svc 0"
    r_pending = next(r for r in rows if r["run_id"] == pending)
    assert r_pending["note_reponse"] is None and r_pending["notes"] == 0, "non noté : valeur absente, pas 0"
    history = services.score_rows(db_session, m, history=True)
    assert {r["run_id"] for r in history} == {old, new, other, top, pending}
    assert history[0]["run_id"] == pending, "plus récents d'abord"
    assert len(services.score_rows(db_session, m, history=True, limit=3)) == 3
    assert {r["run_id"] for r in services.score_rows(db_session, s)} == {new, other}, "le service ne voit pas le ministère"


def test_page_et_routes_de_l_explorateur(client, db_session, monde):
    m, s = monde["m"], monde["s"]
    (ia1, _), judges = monde["tested"], monde["judges"]
    peri_s, q_s = _corpus(db_session, s, "svc")
    run_id = _run(db_session, s, peri_s, q_s, ia1, judges, 7)
    page = client.get(f"/o/{m.slug}/explore")
    assert page.status_code == 200 and "Explorer les scores" in page.text
    for needle in (f"/o/{m.slug}/api/stats/scores", "dsfr-data-facets", "dsfr-data-search", "dsfr-data-list",
                   "dsfr-data-pivot", 'fields="entite, perimetre, ia"', "/static/vendor/dsfr-data-"):
        assert needle in page.text, needle
    assert f"/o/{m.slug}/api/stats/scores?history=1" in client.get(f"/o/{m.slug}/explore", params={"history": 1}).text
    data = client.get(f"/o/{m.slug}/api/stats/scores").json()
    assert {r["run_id"] for r in data} == {run_id} and data[0]["entite"] == "Service Expl"
    api = client.get(f"/api/v1/orgs/{m.slug}/stats/scores")
    assert api.status_code == 200 and [r["run_id"] for r in api.json()] == [run_id, run_id]
    assert client.get(f"/api/v1/orgs/{m.slug}/stats/scores", params={"history": "true"}).status_code == 200
    assert 'href="/o/' + m.slug + '/explore"' in client.get(f"/o/{m.slug}/").text, "lien dans le menu principal"


def test_scores_en_lecture_publique(anonymous_client, db_session, monde):
    s = monde["s"]
    peri_s, q_s = _corpus(db_session, s, "svc")
    _run(db_session, s, peri_s, q_s, monde["tested"][0], monde["judges"], 5)
    r = anonymous_client.get(f"/api/v1/orgs/{s.slug}/stats/scores")
    assert r.status_code == 200 and len(r.json()) == 2, "lecture publique, comme les autres statistiques"
