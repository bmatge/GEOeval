"""Suite E8 sur PostgreSQL : rejugement versionné et calibration des notateurs."""
from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from geoeval.core import evaluate
from geoeval.db.models import (
    EvaluationBatch,
    EvaluationPrompt,
    GoldAnnotation,
    Job,
    Model,
    Organization,
    Perimeter,
    QuestionTheme,
    RejudgeEvaluation,
    RunEvaluation,
    RunResult,
    RunRow,
    Theme,
    User,
)
from geoeval.web import agreement, api_tokens, budget, calibration, detectors, launching, notifications, rejudge, services, tenancy
from geoeval.worker import jobs
from tests.conftest import TEST_USER_EMAIL

pytestmark = pytest.mark.integration

PREFIX = "e8s-"
DOMAIN = "@e8s.test"


@pytest.fixture()
def clean(db_session):
    def _purge():
        ids = [o.id for o in db_session.execute(select(Organization).where(Organization.slug.like(PREFIX + "%"))).scalars()]
        p = {"ids": ids or [-1], "d": "%" + DOMAIN, "t": PREFIX + "%"}
        tests_of = "SELECT test_id FROM tests WHERE organization_id = ANY(:ids)"
        runs_of = "SELECT run_id FROM runs WHERE organization_id = ANY(:ids)"
        db_session.execute(text(f"DELETE FROM gold_annotations WHERE organization_id = ANY(:ids) OR run_id IN ({runs_of})"), p)
        db_session.execute(text("DELETE FROM evaluation_batches WHERE organization_id = ANY(:ids)"), p)
        db_session.execute(text(f"DELETE FROM rejudge_evaluations WHERE run_id IN ({runs_of})"), p)
        db_session.execute(text(f"DELETE FROM run_evaluations WHERE run_id IN ({runs_of}) OR test_id IN ({tests_of})"), p)
        db_session.execute(text(f"DELETE FROM run_results WHERE run_id IN ({runs_of}) OR test_id IN ({tests_of})"), p)
        db_session.execute(text("DELETE FROM usage WHERE organization_id = ANY(:ids)"), p)
        db_session.execute(text(f"DELETE FROM runs WHERE run_id IN ({runs_of})"), p)
        db_session.execute(text("DELETE FROM job_logs WHERE job_id IN (SELECT id FROM jobs WHERE organization_id = ANY(:ids))"), p)
        db_session.execute(text("DELETE FROM notifications WHERE organization_id = ANY(:ids) "
                                "OR user_id IN (SELECT id FROM users WHERE email LIKE :d)"), p)
        db_session.execute(text(f"DELETE FROM test_themes WHERE test_id IN ({tests_of})"), p)
        db_session.execute(text("DELETE FROM themes WHERE slug LIKE :t"), p)
        db_session.execute(text(f"DELETE FROM test_ground_truth WHERE test_id IN ({tests_of})"), p)
        for tbl, col in (("jobs", "organization_id"), ("budgets", "organization_id"),
                         ("detector_settings", "organization_id"), ("tests", "organization_id"),
                         ("perimeters", "organization_id"), ("api_tokens", "organization_id"),
                         ("memberships", "org_id"), ("audit_log", "org_id")):
            db_session.execute(text(f"DELETE FROM {tbl} WHERE {col} = ANY(:ids)"), p)
        db_session.execute(text("DELETE FROM evaluation_prompts WHERE prompt_name LIKE :t"), p)
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


def _judges(db, n=2):
    js = db.execute(select(Model).where(Model.is_judge.is_(True), Model.is_active.is_(True))
                    .order_by(Model.model_id)).scalars().all()[:n]
    assert len(js) == n, "le seed fournit des notateurs actifs"
    return js


def _tested(db):
    return db.execute(select(Model).where(Model.model_name == "openrouter").order_by(Model.model_id)).scalars().first()


def _question(db, org, peri, prompt):
    return services.create_test(db, org.id, perimeter_id=peri.id, prompt=prompt, expected_answer="Réponse",
                                response_quality_prompt_id=None, citation_quality_prompt_id=None)


def _run(db, org, tests, tested, judge=None, scores=None):
    """Run d'une entité ; `scores[i]` = note d'origine du notateur pour `tests[i]`."""
    r = RunRow(organization_id=org.id, tested_model_id=tested.model_id)
    db.add(r)
    db.commit()
    for i, t in enumerate(tests):
        db.add(RunResult(run_id=r.run_id, test_id=t.test_id, raw_answer=f"réponse {i}", raw_citations=[]))
    db.commit()
    if judge is not None:
        for t, sc in zip(tests, scores):
            db.add(RunEvaluation(run_id=r.run_id, test_id=t.test_id, judge_model_id=judge.model_id, judge_run_index=1,
                                 response_quality_label="conforme" if sc >= 5 else "non_conforme",
                                 response_quality_score=sc, citation_quality_label="conforme",
                                 citation_quality_score=sc))
        db.commit()
    return r.run_id


@pytest.fixture()
def monde(db_session, clean):
    m = tenancy.create_org(db_session, name="Ministère E8s", slug=PREFIX + "min")
    s = tenancy.create_org(db_session, name="Service E8s", slug=PREFIX + "svc", parent=m)
    autre = tenancy.create_org(db_session, name="Autre E8s", slug=PREFIX + "autre")
    peri = Perimeter(organization_id=s.id, name="Site E8s", slug="site-e8s", kind="site")
    db_session.add(peri)
    db_session.commit()
    tests = [_question(db_session, s, peri, f"Question E8s {i}") for i in range(3)]
    return dict(m=m, s=s, autre=autre, peri=peri, tests=tests,
                editor_s=_user(db_session, "editor-s", s, "editor"), viewer_s=_user(db_session, "viewer-s", s, "viewer"),
                editor_m=_user(db_session, "editor-m", m, "editor"))


@pytest.fixture()
def fake_judge(monkeypatch):
    """Doublure des notateurs : note = 9 pour le premier notateur du seed, 3 sinon ; prompts capturés."""
    prompts: list[str] = []

    def _call(judge_model, user_prompt, organization_id=None):
        prompts.append(user_prompt)
        score = 9 if judge_model.model_id == min(j.model_id for j in _ALL_JUDGES) else 3
        return json.dumps({"label": "conforme" if score >= 5 else "non_conforme", "score": score}), None

    monkeypatch.setattr(evaluate, "call_judge_llm", _call)
    monkeypatch.setattr(jobs, "HEARTBEAT_SECONDS", 3600)
    return prompts


_ALL_JUDGES: list[Model] = []


# ---------------------------------------------------------------------
# Rejugement : contrôles, devis, mise en file
# ---------------------------------------------------------------------
def test_rejugement_controles_et_mise_en_file(db_session, monde, monkeypatch):
    s, tests = monde["s"], monde["tests"]
    j1, j2 = _judges(db_session)
    tested = _tested(db_session)
    r1 = _run(db_session, s, tests, tested, j1, [8, 5, 2])
    r_autre = _run(db_session, monde["autre"], tests[:1], tested)
    r_vide = _run(db_session, s, [], tested)
    kw = dict(role="editor", is_platform_admin=False)
    for bad, kind in ((dict(run_ids=[], judge_models=[j2.model_version]), "validation"),
                      (dict(run_ids=[r1], judge_models=[]), "validation"),
                      (dict(run_ids=[r1, r_autre], judge_models=[j2.model_version]), "not_found"),
                      (dict(run_ids=[r1, r_vide], judge_models=[j2.model_version]), "validation"),
                      (dict(run_ids=[r1], judge_models=["inexistant"]), "validation"),
                      (dict(run_ids=[r1], judge_models=[j2.model_version], repeats=9), "validation"),
                      (dict(run_ids=[r1], judge_models=[j2.model_version], citation_prompt_id=1), "validation")):
        with pytest.raises(launching.LaunchError) as e:
            rejudge.prepare(db_session, s, **bad, **kw)
        assert e.value.kind == kind, bad
    # Devis : appels des notateurs seulement (aucune IA évaluée), une ligne par résultat notable.
    seen = {}
    real = rejudge.pricing.estimate_scan_cost

    def spy(session, **kw2):
        seen.update(kw2)
        return {"total_eur": Decimal("5"), "by_model": [], "unpriced": []}
    monkeypatch.setattr(rejudge.pricing, "estimate_scan_cost", spy)
    # Budget consolidé : le plafond de l'entité parente bloque aussi le rejugement.
    budget.set_cap(db_session, org_id=monde["m"].id, cap_eur=Decimal("1"), updated_by=None)
    with pytest.raises(launching.LaunchError) as e:
        rejudge.create(db_session, s, run_ids=[r1], judge_models=[j2.model_version], **kw)
    assert e.value.kind == "budget"
    assert seen["tested_models"] == [] and len(seen["tests"]) == 3
    assert seen["judges"] == [{"model": j2.model_version, "repeats": 1}]
    budget.set_cap(db_session, org_id=monde["m"].id, cap_eur=Decimal("1000"), updated_by=None)
    monkeypatch.setattr(rejudge.pricing, "estimate_scan_cost", real)
    b = rejudge.create(db_session, s, run_ids=[r1, r1], judge_models=[j2.model_version], repeats=2, label=" v2 ",
                       created_by=monde["editor_s"].id, **kw)
    assert (b.run_ids, b.label, b.judges) == ([r1], "v2", [{"model_id": j2.model_id, "model_version": j2.model_version,
                                                            "repeats": 2}])
    t0 = tests[0]
    assert str(t0.response_quality_prompt_id) in b.grids and len(b.grids[str(t0.response_quality_prompt_id)]["sha256"]) == 64
    job = db_session.get(Job, b.job_id)
    assert job.status == "queued" and job.params["kind"] == "rejudge" and job.params["batch_id"] == b.id
    assert rejudge.status(db_session, b) == "queued"
    assert [x.id for x in rejudge.list_for_org(db_session, s)] == [b.id]
    with pytest.raises(launching.LaunchError):
        rejudge.get_for_org(db_session, monde["autre"], b.id)


def test_rejugement_execute_par_le_worker_sans_toucher_l_origine(db_session, monde, fake_judge):
    s, tests = monde["s"], monde["tests"]
    j1, j2 = _judges(db_session)
    _ALL_JUDGES[:] = [j1, j2]
    tested = _tested(db_session)
    r1 = _run(db_session, s, tests, tested, j1, [8, 5, 2])
    grid = EvaluationPrompt(prompt_type_id=1, prompt_name=PREFIX + "grille-v2", prompt_text="GRILLE-E8S-V2 : note sévère.")
    db_session.add(grid)
    db_session.commit()
    before = db_session.execute(select(RunEvaluation.test_id, RunEvaluation.response_quality_score)
                                .where(RunEvaluation.run_id == r1).order_by(RunEvaluation.test_id)).all()
    b = rejudge.create(db_session, s, run_ids=[r1], judge_models=[j2.model_version], response_prompt_id=grid.prompt_id,
                       role="editor", is_platform_admin=False)
    db_session.execute(text("UPDATE jobs SET status = 'running' WHERE id = :id"), {"id": b.job_id})
    db_session.commit()
    jobs.execute(b.job_id)
    db_session.expire_all()
    job = db_session.get(Job, b.job_id)
    assert job.status == "done", job.error
    assert any("GRILLE-E8S-V2" in pr for pr in fake_judge), "grille imposée transmise au notateur"
    rows = db_session.execute(select(RejudgeEvaluation).where(RejudgeEvaluation.batch_id == b.id)).scalars().all()
    assert len(rows) == 3 and {r.judge_model_id for r in rows} == {j2.model_id}
    assert {float(r.response_quality_score) for r in rows} == {3.0}
    after = db_session.execute(select(RunEvaluation.test_id, RunEvaluation.response_quality_score)
                               .where(RunEvaluation.run_id == r1).order_by(RunEvaluation.test_id)).all()
    assert after == before, "les notes d'origine ne bougent pas"
    cmp = rejudge.compare(db_session, b)
    assert cmp.summary["n_pairs"] == 3 and cmp.summary["orig_response"] == pytest.approx(5.0)
    assert cmp.summary["new_response"] == pytest.approx(3.0) and cmp.summary["delta_response"] == pytest.approx(-2.0)
    assert cmp.summary["abs_delta_response"] == pytest.approx((5 + 2 + 1) / 3)
    assert [p["test_id"] for p in cmp.pairs][0] == tests[0].test_id, "plus grand écart d'abord (8 → 3)"
    assert cmp.by_model[0]["tested_model"] == tested.model_version
    assert rejudge.runs_with_batches(db_session, [r1]) == {r1: [b.id]}


# ---------------------------------------------------------------------
# Calibration : annotation, portée héritée, accord, notification
# ---------------------------------------------------------------------
def test_annotation_dans_l_application(db_session, monde):
    s, tests = monde["s"], monde["tests"]
    tested = _tested(db_session)
    r1 = _run(db_session, s, tests, tested)
    ed = monde["editor_s"]
    kw = dict(user_id=ed.id, email=ed.email, response_label="partiel", response_score="6,5",
              citation_label="conforme", citation_score=8)
    a = calibration.annotate(db_session, s, run_id=r1, test_id=tests[0].test_id, **kw)
    assert (a.organization_id, a.annotator_user_id, a.response_score, a.response_label) == (s.id, ed.id, Decimal("6.50"), "partiel")
    again = calibration.annotate(db_session, s, run_id=r1, test_id=tests[0].test_id, **{**kw, "response_score": 7})
    assert again.id == a.id and again.response_score == Decimal("7.00"), "une annotation par personne et résultat"
    assert calibration.annotated_test_ids(db_session, r1) == {tests[0].test_id: 1}
    for bad, status in ((dict(run_id=r1, test_id=-1), 404), (dict(run_id=_run(db_session, monde["autre"], tests, tested),
                                                                    test_id=tests[0].test_id), 404)):
        with pytest.raises(calibration.CalibrationError) as e:
            calibration.annotate(db_session, s, **bad, **kw)
        assert e.value.status == status
    for bad in (dict(response_label="bof"), dict(response_score="11"), dict(citation_score="x")):
        with pytest.raises(calibration.CalibrationError):
            calibration.annotate(db_session, s, run_id=r1, test_id=tests[1].test_id, **{**kw, **bad})


def _annotate(db, org, run_id, test, score, email):
    db.add(GoldAnnotation(test_id=test.test_id, run_id=run_id, annotator_email=email, organization_id=org.id if org else None,
                          response_label="conforme" if score >= 5 else "non_conforme", response_score=score,
                          citation_label="conforme", citation_score=score))
    db.commit()


def test_accord_portee_heritee_theme_et_notification(db_session, monde, monkeypatch):
    m, s, editor = monde["m"], monde["s"], monde["editor_s"]
    monkeypatch.setenv("GEOEVAL_CALIBRATION_MIN_PAIRS", "3")
    j1, j2 = _judges(db_session)
    tested = _tested(db_session)
    peri_m = Perimeter(organization_id=m.id, name="Site min E8s", slug="site-min-e8s", kind="site")
    db_session.add(peri_m)
    db_session.commit()
    tm = [_question(db_session, m, peri_m, f"Question min E8s {i}") for i in range(2)]
    ts = monde["tests"]
    # Notes d'origine de j1 parfaitement alignées sur le gold (2, 4, 6, 8, 10).
    rm = _run(db_session, m, tm, tested, j1, [2, 4])
    rs = _run(db_session, s, ts, tested, j1, [6, 8, 10])
    platform_before = agreement.compute_agreement_vs_gold(db_session, j1.model_id)["n_pairs"]
    for t, sc in zip(tm, [2, 4]):
        _annotate(db_session, m, rm, t, sc, "a" + DOMAIN)
    for t, sc in zip(ts[:2], [6, 8]):
        _annotate(db_session, s, rs, t, sc, "b" + DOMAIN)
    _annotate(db_session, None, rs, ts[2], 10, "plateforme" + DOMAIN)       # gold set importé : vaut pour tous
    assert len(calibration.gold_pairs(db_session, s)) == 5, "service : les siennes + le ministère + la plateforme"
    assert len(calibration.gold_pairs(db_session, m)) == 3, "ministère : pas celles du service"
    assert agreement.compute_agreement_vs_gold(db_session, j1.model_id)["n_pairs"] == platform_before + 1, \
        "la page méthodologie de la plateforme ne lit que le gold set importé"
    theme = Theme(slug=PREFIX + "fiscalite", label="Fiscalité E8s")
    db_session.add(theme)
    db_session.commit()
    for t in [tm[0], ts[0], ts[1]]:
        db_session.add(QuestionTheme(test_id=t.test_id, theme_id=theme.id))
    db_session.commit()
    ag = calibration.agreement_for(db_session, s, model_id=j1.model_id)
    assert ag.overall["n_pairs"] == 5 and ag.overall["response_spearman"] == pytest.approx(1.0)
    assert ag.overall["response_mae"] == pytest.approx(0.0) and ag.overall["response_kappa"] == pytest.approx(1.0)
    [th] = ag.by_theme
    assert (th["label"], th["n_pairs"]) == ("Fiscalité E8s", 3)
    assert detectors.check_judge_disagreement(db_session, s, [j1.model_id]) == 0, "accord parfait"
    # Lot de rejugement : j2 note à l'envers du gold → corrélation −1.
    b = EvaluationBatch(organization_id=s.id, run_ids=[rm, rs], judges=[{"model_id": j2.model_id,
                        "model_version": j2.model_version, "repeats": 1}])
    db_session.add(b)
    db_session.commit()
    for run_id, t, sc in [(rm, tm[0], 10), (rm, tm[1], 8), (rs, ts[0], 6), (rs, ts[1], 4), (rs, ts[2], 2)]:
        db_session.add(RejudgeEvaluation(batch_id=b.id, run_id=run_id, test_id=t.test_id, judge_model_id=j2.model_id,
                                         judge_run_index=1, response_quality_label="conforme", response_quality_score=sc,
                                         citation_quality_label="conforme", citation_quality_score=sc))
    db_session.commit()
    keys = [a.key for a in calibration.overview(db_session, s)]
    assert keys == [f"orig:{j1.model_id}", f"batch-{b.id}:{j2.model_id}"]
    assert detectors.check_judge_disagreement(db_session, s, [j2.model_id], batch_id=b.id) == 2, "global + thème"
    [n1, n2] = sorted([n for n in notifications.list_for_user(db_session, editor.id) if n.kind == "judge_disagreement"],
                      key=lambda n: n.title)
    assert "tous thèmes" in n1.title + n2.title and "Fiscalité E8s" in n1.title + n2.title
    assert n1.link == f"/o/{s.slug}/calibration" and n1.payload["batch_id"] == b.id
    assert detectors.check_judge_disagreement(db_session, s, [j2.model_id], batch_id=b.id) == 0, "une fois par semaine"
    # Seuil posé par le ministère : le service en hérite.
    detectors.set_settings(db_session, m, runs=None, threshold=None, calibration_min_rho="0.9")
    eff = detectors.effective_settings(db_session, s)
    assert (eff.calibration_min_rho, eff.calibration_from.id) == (Decimal("0.90"), m.id)
    for bad in ("1.5", "-0.1", "x"):
        with pytest.raises(ValueError):
            detectors.set_settings(db_session, s, runs=None, threshold=None, calibration_min_rho=bad)
    assert detectors.calibration_after_evaluation_safely(s.id, [j2.model_version]) == 0, "notes d'origine de j2 : aucune"
    assert detectors.calibration_after_evaluation_safely(None, [j1.model_id]) == 0


# ---------------------------------------------------------------------
# UI et API
# ---------------------------------------------------------------------
def _ci_user(db):
    return db.execute(select(User).where(User.email == TEST_USER_EMAIL)).scalar_one()


def _bearer(db, org, role):
    _, plain = api_tokens.create(db, org_id=org.id, name=f"tok-{role}", role=role, created_by=None)
    return {"Authorization": f"Bearer {plain}"}


def test_ui_rejugement_et_calibration(client, db_session, monde):
    client.get("/")
    me = _ci_user(db_session)
    s, tests = monde["s"], monde["tests"]
    tenancy.add_membership(db_session, user_id=me.id, org_id=s.id, role="org_admin")
    j1, j2 = _judges(db_session)
    r1 = _run(db_session, s, tests, _tested(db_session), j1, [8, 5, 2])
    page = client.get(f"/o/{s.slug}/runs/{r1}")
    assert page.status_code == 200 and f"/rejudge/new?run_id={r1}" in page.text
    assert f"/runs/{r1}/tests/{tests[0].test_id}/annotate" in page.text
    form = client.get(f"/o/{s.slug}/rejudge/new", params={"run_id": r1})
    assert form.status_code == 200 and f'value="{r1}" checked' in form.text and j2.model_version in form.text
    assert client.post(f"/o/{s.slug}/rejudge/new", data={"run_ids": [str(r1)]}).status_code == 400
    r = client.post(f"/o/{s.slug}/rejudge/new", data={"run_ids": [str(r1)], "judge_models": [j2.model_version],
                                                      "repeats": "1", "label": "Essai UI"})
    assert r.status_code == 303
    b = db_session.execute(select(EvaluationBatch).where(EvaluationBatch.organization_id == s.id)).scalar_one()
    assert r.headers["location"] == f"/o/{s.slug}/rejudge/{b.id}"
    db_session.add(RejudgeEvaluation(batch_id=b.id, run_id=r1, test_id=tests[0].test_id, judge_model_id=j2.model_id,
                                     judge_run_index=1, response_quality_score=4, citation_quality_score=4))
    db_session.commit()
    detail = client.get(f"/o/{s.slug}/rejudge/{b.id}")
    assert detail.status_code == 200 and "Essai UI" in detail.text and "8.00 → 4.00" in detail.text
    assert "Ensemble" in detail.text and "-4.00" in detail.text
    lst = client.get(f"/o/{s.slug}/rejudge")
    assert lst.status_code == 200 and f"/rejudge/{b.id}" in lst.text and "en file" in lst.text
    assert client.get(f"/o/{monde['autre'].slug}/rejudge/{b.id}").status_code in (403, 404)
    # Annotation puis page de calibration.
    url = f"/o/{s.slug}/runs/{r1}/tests/{tests[0].test_id}/annotate"
    assert client.get(url).status_code == 200
    r = client.post(url, data={"response_label": "conforme", "response_score": "9", "citation_label": "partiel",
                               "citation_score": "5", "notes": "ok"})
    assert r.status_code == 303 and r.headers["location"] == f"/o/{s.slug}/runs/{r1}"
    assert 'value="9.00"' in client.get(url).text, "l'annotation existante pré-remplit le formulaire"
    assert client.post(url, data={"response_label": "bof", "response_score": "9", "citation_label": "partiel",
                                  "citation_score": "5"}).status_code == 400
    cal = client.get(f"/o/{s.slug}/calibration")
    assert cal.status_code == 200 and "1 paire(s)" in cal.text and TEST_USER_EMAIL in cal.text
    assert client.post(f"/o/{s.slug}/settings/detectors", data={"calibration_min_rho": "0.7"}).status_code == 303
    page = client.get(f"/o/{s.slug}/settings/detectors")
    assert page.status_code == 200 and "au moins 0.70" in page.text
    assert client.post(f"/o/{s.slug}/settings/detectors", data={"calibration_min_rho": "2"}).status_code == 400


def test_api_rejugement_et_calibration(client, db_session, monde):
    client.get("/")
    me = _ci_user(db_session)
    s, tests = monde["s"], monde["tests"]
    j1, j2 = _judges(db_session)
    r1 = _run(db_session, s, tests, _tested(db_session), j1, [8, 5, 2])
    hv, he, ha = (_bearer(db_session, s, role) for role in ("viewer", "editor", "org_admin"))
    base = f"/api/v1/orgs/{s.slug}"
    assert client.post(f"{base}/rejudge", headers=hv, json={"run_ids": [r1], "judge_models": [j2.model_version]}).status_code == 403
    r = client.post(f"{base}/rejudge", headers=he, json={"run_ids": [r1], "judge_models": ["inexistant"]})
    assert r.status_code == 400 and r.headers["content-type"].startswith("application/problem+json")
    r = client.post(f"{base}/rejudge", headers=he, json={"run_ids": [r1], "judge_models": [j2.model_version], "repeats": 2})
    assert r.status_code == 201 and r.json()["status"] == "queued" and r.json()["judges"][0]["repeats"] == 2
    bid = r.json()["id"]
    assert [b["id"] for b in client.get(f"{base}/rejudge", headers=hv).json()] == [bid]
    detail = client.get(f"{base}/rejudge/{bid}", headers=hv).json()
    assert detail["summary"]["n_pairs"] == 0 and detail["pairs"] == []
    assert client.get(f"/api/v1/orgs/{monde['autre'].slug}/rejudge/{bid}",
                      headers=_bearer(db_session, monde["autre"], "viewer")).status_code == 404
    # Annotation : session seulement (le client de test est un admin plateforme connecté).
    body = {"run_id": r1, "test_id": tests[1].test_id, "response_label": "partiel", "response_score": 5,
            "citation_label": "conforme", "citation_score": 7}
    assert client.put(f"{base}/annotations", headers=ha, json=body).status_code == 403, "jeton : pas d'annotateur"
    r = client.put(f"{base}/annotations", json=body)
    assert r.status_code == 200 and r.json()["annotator_email"] == me.email and r.json()["organization_id"] == s.id
    assert client.put(f"{base}/annotations", json={**body, "response_label": "bof"}).status_code == 422
    assert client.put(f"{base}/annotations", json={**body, "test_id": -1}).status_code == 404
    assert [a["test_id"] for a in client.get(f"{base}/annotations", headers=hv).json()] == [tests[1].test_id]
    cal = client.get(f"{base}/calibration", headers=hv).json()
    assert cal["n_gold_pairs"] == 1 and cal["threshold"] == "0.5" and cal["min_pairs"] == 10
    [j] = cal["judges"]
    assert (j["model_version"], j["batch_id"], j["overall"]["n_pairs"], j["below_threshold"]) == (j1.model_version, None, 1, False)
    ds = client.put(f"{base}/detector-settings", headers=ha, json={"calibration_min_rho": "0.8"}).json()
    assert ds["effective_calibration_min_rho"] == "0.80" and ds["calibration_from_org_slug"] == s.slug
    assert client.put(f"{base}/detector-settings", headers=ha, json={"calibration_min_rho": 3}).status_code == 422
