"""Fin de E8 sur PostgreSQL : promotion réversible d'un lot rejugé, validation métier des questions."""
from __future__ import annotations

import pytest
from sqlalchemy import select, text

from geoeval.db.models import (
    Campaign,
    EvaluationBatch,
    Job,
    Model,
    Organization,
    Perimeter,
    RejudgeEvaluation,
    RunEvaluation,
    RunResult,
    RunRow,
    User,
)
from geoeval.web import api_tokens, launching, notifications, pools, rejudge, reviews, services, tenancy
from tests.conftest import TEST_USER_EMAIL

pytestmark = pytest.mark.integration

PREFIX = "e8f-"
DOMAIN = "@e8f.test"


@pytest.fixture()
def clean(db_session):
    def _purge():
        ids = [o.id for o in db_session.execute(select(Organization).where(Organization.slug.like(PREFIX + "%"))).scalars()]
        p = {"ids": ids or [-1], "d": "%" + DOMAIN}
        tests_of = "SELECT test_id FROM tests WHERE organization_id = ANY(:ids)"
        runs_of = "SELECT run_id FROM runs WHERE organization_id = ANY(:ids)"
        db_session.execute(text(f"UPDATE runs SET reference_batch_id = NULL, origin_batch_id = NULL WHERE run_id IN ({runs_of})"), p)
        db_session.execute(text("DELETE FROM evaluation_batches WHERE organization_id = ANY(:ids)"), p)
        db_session.execute(text(f"DELETE FROM run_evaluations WHERE run_id IN ({runs_of}) OR test_id IN ({tests_of})"), p)
        db_session.execute(text(f"DELETE FROM run_results WHERE run_id IN ({runs_of}) OR test_id IN ({tests_of})"), p)
        db_session.execute(text(f"DELETE FROM runs WHERE run_id IN ({runs_of})"), p)
        db_session.execute(text("DELETE FROM campaigns WHERE owner_org_id = ANY(:ids)"), p)
        db_session.execute(text("DELETE FROM question_pools WHERE owner_org_id = ANY(:ids)"), p)
        db_session.execute(text("DELETE FROM job_logs WHERE job_id IN (SELECT id FROM jobs WHERE organization_id = ANY(:ids))"), p)
        db_session.execute(text("DELETE FROM notifications WHERE organization_id = ANY(:ids) "
                                "OR user_id IN (SELECT id FROM users WHERE email LIKE :d)"), p)
        db_session.execute(text(f"DELETE FROM test_ground_truth WHERE test_id IN ({tests_of})"), p)
        for tbl, col in (("jobs", "organization_id"), ("tests", "organization_id"), ("perimeters", "organization_id"),
                         ("api_tokens", "organization_id"), ("memberships", "org_id"), ("audit_log", "org_id")):
            db_session.execute(text(f"DELETE FROM {tbl} WHERE {col} = ANY(:ids)"), p)
        db_session.execute(text("UPDATE organizations SET parent_id = NULL WHERE id = ANY(:ids)"), p)
        db_session.execute(text("DELETE FROM organizations WHERE id = ANY(:ids)"), p)
        db_session.execute(text("DELETE FROM users WHERE email LIKE :d"), p)
        db_session.commit()
    _purge()
    yield
    _purge()


def _user(db, name, org=None, role=None):
    u = User(email=name + DOMAIN)
    db.add(u)
    db.commit()
    if org is not None:
        tenancy.add_membership(db, user_id=u.id, org_id=org.id, role=role)
    return u


@pytest.fixture()
def monde(db_session, clean):
    m = tenancy.create_org(db_session, name="Ministère E8f", slug=PREFIX + "min")
    s = tenancy.create_org(db_session, name="Service E8f", slug=PREFIX + "svc", parent=m)
    peri = Perimeter(organization_id=s.id, name="Site E8f", slug="site-e8f", kind="site")
    db_session.add(peri)
    db_session.commit()
    return dict(m=m, s=s, peri=peri, admin_m=_user(db_session, "admin-m", m, "org_admin"),
                editor_s=_user(db_session, "editor-s", s, "editor"), editor2_s=_user(db_session, "editor2-s", s, "editor"),
                viewer_s=_user(db_session, "viewer-s", s, "viewer"))


def _judges(db):
    js = db.execute(select(Model).where(Model.is_judge.is_(True), Model.is_active.is_(True))
                    .order_by(Model.model_id)).scalars().all()[:2]
    assert len(js) == 2
    return js


def _tested(db):
    return db.execute(select(Model).where(Model.model_name == "openrouter").order_by(Model.model_id)).scalars().first()


def _q(db, org, peri, prompt="Quel est le délai ?", status="published", created_by=None):
    return services.create_test(db, org.id, perimeter_id=peri.id, prompt=prompt, expected_answer="Deux mois",
                                response_quality_prompt_id=None, citation_quality_prompt_id=None, status=status,
                                created_by=created_by)


def _run(db, org, tests, tested, judge, scores, campaign_id=None):
    r = RunRow(organization_id=org.id, tested_model_id=tested.model_id, campaign_id=campaign_id)
    db.add(r)
    db.commit()
    for t, sc in zip(tests, scores):
        db.add(RunResult(run_id=r.run_id, test_id=t.test_id, raw_answer="…", raw_citations=[]))
        db.add(RunEvaluation(run_id=r.run_id, test_id=t.test_id, judge_model_id=judge.model_id, judge_run_index=1,
                             response_quality_label="conforme", response_quality_score=sc,
                             citation_quality_label="conforme", citation_quality_score=sc))
    db.commit()
    return r.run_id


def _batch(db, org, run_ids, judge, scores_by_run, *, done=True):
    """Lot de rejugement terminé (job 'done') avec ses notes."""
    job = Job(id=f"j{org.id}-{len(run_ids)}-{judge.model_id}-{sum(sum(v) for v in scores_by_run.values())}",
              organization_id=org.id, status="done" if done else "running", params={}, run_ids=run_ids)
    db.add(job)
    b = EvaluationBatch(organization_id=org.id, run_ids=run_ids, judges=[{"model_id": judge.model_id,
                        "model_version": judge.model_version, "repeats": 1}], job_id=job.id)
    db.add(b)
    db.commit()
    for run_id, scores in scores_by_run.items():
        tids = db.execute(select(RunResult.test_id).where(RunResult.run_id == run_id).order_by(RunResult.test_id)).scalars()
        for tid, sc in zip(tids, scores):
            db.add(RejudgeEvaluation(batch_id=b.id, run_id=run_id, test_id=tid, judge_model_id=judge.model_id,
                                     judge_run_index=1, response_quality_label="partiel", response_quality_score=sc,
                                     citation_quality_label="partiel", citation_quality_score=sc))
    db.commit()
    return b


def _official(db, run_id):
    return db.execute(select(RunEvaluation.judge_model_id, RunEvaluation.response_quality_score)
                      .where(RunEvaluation.run_id == run_id).order_by(RunEvaluation.test_id)).all()


# ---------------------------------------------------------------------
# Promotion réversible
# ---------------------------------------------------------------------
def test_promotion_echange_reversible_hors_campagne(db_session, monde):
    s, peri = monde["s"], monde["peri"]
    j1, j2 = _judges(db_session)
    tested = _tested(db_session)
    t1, t2 = _q(db_session, s, peri, "Q1"), _q(db_session, s, peri, "Q2")
    r1 = _run(db_session, s, [t1, t2], tested, j1, [8, 6])
    camp = Campaign(owner_org_id=s.id, name="Camp E8f", status="active", schedule_kind="once", schedule_config={})
    db_session.add(camp)
    db_session.commit()
    r_camp = _run(db_session, s, [t1], tested, j1, [7], campaign_id=camp.id)
    r_vide = _run(db_session, s, [t2], tested, j1, [5])
    before = _official(db_session, r1)
    avg_before = services.list_runs(db_session, s.id)
    b = _batch(db_session, s, [r1, r_camp, r_vide], j2, {r1: [3, 2], r_camp: [1]})
    res = rejudge.promote(db_session, s, b, user_id=monde["admin_m"].id)
    assert res.done == [r1]
    assert dict(res.skipped) == {r_camp: "run de campagne : son protocole fige les notateurs",
                                 r_vide: "aucune note dans ce lot"}
    assert [(j, float(sc)) for j, sc in _official(db_session, r1)] == [(j2.model_id, 3.0), (j2.model_id, 2.0)]
    assert [(j, float(sc)) for j, sc in _official(db_session, r_camp)] == [(j1.model_id, 7.0)], "campagne intacte"
    db_session.expire_all()
    run = db_session.get(RunRow, r1)
    assert run.reference_batch_id == b.id and run.origin_batch_id is not None
    archive = db_session.get(EvaluationBatch, run.origin_batch_id)
    assert archive.kind == "origin" and archive.run_ids == [r1]
    archived = db_session.execute(select(RejudgeEvaluation.response_quality_score).where(
        RejudgeEvaluation.batch_id == archive.id).order_by(RejudgeEvaluation.test_id)).scalars().all()
    assert [float(x) for x in archived] == [8.0, 6.0], "notes d'origine archivées, pas perdues"
    # Les lectures existantes suivent sans changement (moyenne du run = notes du lot).
    after = {r["run_id"]: r for r in services.list_runs(db_session, s.id)}
    assert after[r1]["avg_response"] == pytest.approx(2.5)
    assert {r["run_id"]: r["avg_response"] for r in avg_before}[r1] == pytest.approx(7.0)
    # La comparaison reste faite contre les notes d'origine (archive), pas contre elles-mêmes.
    cmp = rejudge.compare(db_session, b)
    assert cmp.summary["orig_response"] == pytest.approx((8 + 6 + 7) / 3)
    assert [x.id for x in rejudge.list_for_org(db_session, s)] == [b.id], "l'archive n'est pas un lot listé"
    with pytest.raises(launching.LaunchError):
        rejudge.get_for_org(db_session, s, archive.id)
    assert rejudge.promote(db_session, s, b, user_id=None).done == [], "déjà promu"
    # Un second lot promu sur le même run réutilise l'archive existante.
    b2 = _batch(db_session, s, [r1], j1, {r1: [9, 9]})
    assert rejudge.promote(db_session, s, b2, user_id=None).done == [r1]
    db_session.expire_all()
    assert db_session.get(RunRow, r1).origin_batch_id == archive.id
    assert rejudge.promoted_runs(db_session, b) == [] and rejudge.promoted_runs(db_session, b2) == [r1]
    # Retour arrière : notes d'origine restaurées.
    assert rejudge.revert(db_session, s, b2) == [r1]
    assert _official(db_session, r1) == before
    db_session.expire_all()
    assert db_session.get(RunRow, r1).reference_batch_id is None and db_session.get(EvaluationBatch, b2.id).promoted_at is None
    # Lot non terminé : refus.
    b3 = _batch(db_session, s, [r1], j2, {r1: [1, 1]}, done=False)
    with pytest.raises(launching.LaunchError) as e:
        rejudge.promote(db_session, s, b3, user_id=None)
    assert e.value.status == 409


# ---------------------------------------------------------------------
# Validation métier
# ---------------------------------------------------------------------
def test_validation_metier_quatre_yeux(db_session, monde):
    m, s, peri = monde["m"], monde["s"], monde["peri"]
    ed, ed2, admin_m = monde["editor_s"], monde["editor2_s"], monde["admin_m"]
    # Sans réglage : cycle inchangé.
    q0 = _q(db_session, s, peri, "Sans validation", created_by=ed.id)
    assert q0.status == "published" and not reviews.required(db_session, s)
    # Réglage posé sur le ministère, hérité par le service.
    reviews.set_required(db_session, m, True)
    assert reviews.required_source(db_session, s) == (True, m)
    q = _q(db_session, s, peri, "Nouvelle question", created_by=ed.id)
    assert q.status == "in_review" and q.submitted_by == ed.id
    [n] = [n for n in notifications.list_for_user(db_session, admin_m.id) if n.kind == "review_requested"]
    assert n.link == f"/o/{s.slug}/reviews", "pas de validateur dans le service : repli sur l'admin du ministère"
    assert [t.test_id for t in reviews.pending(db_session, s)] == [q.test_id]
    assert q.test_id not in {t.test_id for t in launching.tests_for_run(db_session, s.id, perimeter_id=peri.id)}, \
        "en relecture : hors runs"
    pool = pools.create(db_session, s, name="Pool E8f")
    with pytest.raises(pools.PoolError):
        pools.add_tests(db_session, pool, [q.test_id])
    # Quatre yeux et droits.
    with pytest.raises(reviews.ReviewError) as e:
        reviews.approve(db_session, s, q, ed2.id)
    assert e.value.status == 403, "un éditeur non désigné n'est pas validateur"
    reviews.set_validator(db_session, s, ed.id, True)
    with pytest.raises(reviews.ReviewError) as e:
        reviews.approve(db_session, s, q, ed.id)
    assert "Quatre yeux" in e.value.detail
    with pytest.raises(reviews.ReviewError):
        reviews.reject(db_session, s, q, admin_m.id, "  ")
    reviews.reject(db_session, s, q, admin_m.id, "Réponse attendue imprécise.")
    assert (q.status, q.review_comment) == ("draft", "Réponse attendue imprécise.")
    [back] = [n for n in notifications.list_for_user(db_session, ed.id) if n.kind == "review_done"]
    assert "renvoyée" in back.title and "imprécise" in back.body
    # Resoumission par l'éditeur 2, approbation par l'éditeur 1 désigné validateur.
    services.publish_test(db_session, s.id, q.test_id, user_id=ed2.id)
    db_session.refresh(q)
    assert q.status == "in_review" and q.submitted_by == ed2.id
    assert [u.id for u in reviews.validators(db_session, s)] == [ed.id], "validateurs du service d'abord"
    reviews.approve(db_session, s, q, ed.id)
    assert (q.status, q.reviewed_by, q.review_comment) == ("published", ed.id, None)
    with pytest.raises(reviews.ReviewError) as e:
        reviews.approve(db_session, s, q, admin_m.id)
    assert e.value.status == 409
    # Re-relecture : modifier l'énoncé d'une question publiée la renvoie en relecture ; la grille, non.
    services.update_test(db_session, s.id, q.test_id, prompt=q.prompt, expected_answer=q.expected_answer,
                         response_quality_prompt_id=None, citation_quality_prompt_id=None, user_id=ed.id)
    db_session.refresh(q)
    assert q.status == "published", "rien de modifié dans l'énoncé"
    services.update_test(db_session, s.id, q.test_id, prompt=q.prompt, expected_answer="Trois mois",
                         response_quality_prompt_id=None, citation_quality_prompt_id=None, user_id=ed.id)
    db_session.refresh(q)
    assert (q.status, q.submitted_by) == ("in_review", ed.id)
    # Réactivation d'une question retirée : relecture aussi.
    services.deactivate_test(db_session, s.id, q0.test_id)
    services.reactivate_test(db_session, s.id, q0.test_id, user_id=ed2.id)
    db_session.refresh(q0)
    assert (q0.status, q0.validity_end_at) == ("in_review", None)
    reviews.approve(db_session, s, q0, admin_m.id)
    assert q0.status == "published"
    # Réglage propre du service à « non » : l'héritage est coupé.
    reviews.set_required(db_session, s, False)
    assert reviews.required_source(db_session, s) == (False, s)
    assert _q(db_session, s, peri, "Libre à nouveau").status == "published"
    with pytest.raises(reviews.ReviewError):
        reviews.set_validator(db_session, s, admin_m.id, True)   # pas membre direct du service


# ---------------------------------------------------------------------
# UI et API
# ---------------------------------------------------------------------
def _ci_user(db):
    return db.execute(select(User).where(User.email == TEST_USER_EMAIL)).scalar_one()


def _bearer(db, org, role):
    _, plain = api_tokens.create(db, org_id=org.id, name=f"tok-{role}", role=role, created_by=None)
    return {"Authorization": f"Bearer {plain}"}


def test_ui_promotion_et_relectures(client, db_session, monde):
    client.get("/")
    me = _ci_user(db_session)
    s, peri = monde["s"], monde["peri"]
    tenancy.add_membership(db_session, user_id=me.id, org_id=s.id, role="org_admin")
    j1, j2 = _judges(db_session)
    t1 = _q(db_session, s, peri, "Q UI")
    r1 = _run(db_session, s, [t1], _tested(db_session), j1, [8])
    b = _batch(db_session, s, [r1], j2, {r1: [4]})
    page = client.get(f"/o/{s.slug}/rejudge/{b.id}")
    assert page.status_code == 200 and "Promouvoir comme référence" in page.text
    assert client.post(f"/o/{s.slug}/rejudge/{b.id}/promote").status_code == 303
    run_page = client.get(f"/o/{s.slug}/runs/{r1}")
    assert run_page.status_code == 200 and f"/rejudge/{b.id}" in run_page.text and "Notes officielles" in run_page.text
    detail = client.get(f"/o/{s.slug}/rejudge/{b.id}")
    assert "Revenir aux notes d" in detail.text and "8.00 → 4.00" in detail.text
    assert client.post(f"/o/{s.slug}/rejudge/{b.id}/revert").status_code == 303
    assert [float(sc) for _, sc in _official(db_session, r1)] == [8.0]
    # Relectures : réglage, validateurs, soumission d'un autre éditeur, approbation par le CI (admin).
    r = client.post(f"/o/{s.slug}/settings/reviews", data={"review_required": "yes",
                                                           "validator_ids": [str(monde["editor_s"].id)]})
    assert r.status_code == 303
    db_session.expire_all()
    assert reviews.required(db_session, s) and reviews.designated(db_session, s) == {monde["editor_s"].id}
    settings_page = client.get(f"/o/{s.slug}/settings/reviews")
    assert settings_page.status_code == 200 and "Réglage effectif" in settings_page.text
    assert client.post(f"/o/{s.slug}/settings/reviews", data={"review_required": "peut-être"}).status_code == 400
    q = _q(db_session, s, peri, "Question UI à relire", created_by=monde["editor2_s"].id)
    lst = client.get(f"/o/{s.slug}/reviews")
    assert lst.status_code == 200 and "Question UI à relire" in lst.text and "Approuver" in lst.text
    _q(db_session, s, peri, "Brouillon UI", status="draft")
    assert "Soumettre en relecture" in client.get(f"/o/{s.slug}/tests").text
    assert client.post(f"/o/{s.slug}/tests/{q.test_id}/reject", data={"comment": ""}).status_code == 400
    assert client.post(f"/o/{s.slug}/tests/{q.test_id}/approve").status_code == 303
    db_session.refresh(q)
    assert q.status == "published"
    # Le CI crée lui-même une question : il ne peut pas l'approuver (quatre yeux).
    r = client.post(f"/o/{s.slug}/tests/new", data={"perimeter_id": str(peri.id), "prompt": "Ma question CI",
                                                   "expected_answer": "x"})
    assert r.status_code == 303
    mine = db_session.execute(select(services.Test).where(services.Test.prompt == "Ma question CI")).scalar_one()
    assert mine.status == "in_review" and mine.submitted_by == me.id
    assert "ta soumission" in client.get(f"/o/{s.slug}/reviews").text
    assert client.post(f"/o/{s.slug}/tests/{mine.test_id}/approve").status_code == 403
    assert "en relecture" in client.get(f"/o/{s.slug}/tests/{mine.test_id}").text


def test_api_promotion_et_relectures(client, db_session, monde):
    client.get("/")
    me = _ci_user(db_session)
    s, peri = monde["s"], monde["peri"]
    j1, j2 = _judges(db_session)
    t1 = _q(db_session, s, peri, "Q API")
    r1 = _run(db_session, s, [t1], _tested(db_session), j1, [8])
    b = _batch(db_session, s, [r1], j2, {r1: [5]})
    base = f"/api/v1/orgs/{s.slug}"
    he, ha = _bearer(db_session, s, "editor"), _bearer(db_session, s, "org_admin")
    assert client.post(f"{base}/rejudge/{b.id}/promote", headers=he).status_code == 403, "org_admin seulement"
    r = client.post(f"{base}/rejudge/{b.id}/promote", headers=ha)
    assert r.status_code == 200 and r.json()["promoted_run_ids"] == [r1] and r.json()["batch"]["promoted_at"]
    assert client.get(f"{base}/rejudge/{b.id}", headers=he).json()["promoted_run_ids"] == [r1]
    assert client.post(f"{base}/rejudge/{b.id}/revert", headers=ha).json()["promoted_run_ids"] == []
    b_run = _batch(db_session, s, [r1], j2, {r1: [1]}, done=False)
    r = client.post(f"{base}/rejudge/{b_run.id}/promote", headers=ha)
    assert r.status_code == 409 and r.headers["content-type"].startswith("application/problem+json")
    # Validation métier.
    assert client.put(f"{base}/review-settings", headers=he, json={"review_required": True}).status_code == 403
    r = client.put(f"{base}/review-settings", headers=ha, json={"review_required": True,
                                                                "validator_user_ids": [monde["editor_s"].id]})
    assert r.status_code == 200 and r.json()["effective_required"] is True and r.json()["source_org_slug"] == s.slug
    assert client.put(f"{base}/review-settings", headers=ha, json={"validator_user_ids": [999999]}).status_code == 400
    r = client.post(f"{base}/questions", headers=he, json={"perimeter_id": peri.id, "prompt": "Q API relue",
                                                           "expected_answer": "Oui"})
    assert r.status_code == 201 and r.json()["status"] == "in_review" and r.json()["is_active"] is False
    qid = r.json()["test_id"]
    assert [x["test_id"] for x in client.get(f"{base}/reviews", headers=he).json()] == [qid]
    assert client.post(f"{base}/questions/{qid}/approve", headers=ha).status_code == 403, "jeton : pas d'identité"
    r = client.post(f"{base}/questions/{qid}/reject", json={"comment": "À préciser"})
    assert r.status_code == 200 and r.json()["status"] == "draft" and r.json()["review_comment"] == "À préciser"
    assert client.post(f"{base}/questions/{qid}/reject", json={"comment": ""}).status_code == 422
    r = client.post(f"{base}/questions/{qid}/publish", headers=he)
    assert r.status_code == 200 and r.json()["status"] == "in_review"
    r = client.post(f"{base}/questions/{qid}/approve")
    assert r.status_code == 200 and r.json()["status"] == "published" and r.json()["is_active"] is True
    assert db_session.get(services.Test, qid).reviewed_by == me.id
