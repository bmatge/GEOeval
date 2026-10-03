"""Pools de questions partagés, thèmes et domaines officiels (E4) sur PostgreSQL."""
from __future__ import annotations

import pytest
from sqlalchemy import select, text

from geoeval.db.models import Model, Organization, Perimeter, RunEvaluation, RunResult, RunRow
from geoeval.web import api_tokens, launching, pools, pricing, services, tenancy, themes
from geoeval.worker import jobs

pytestmark = pytest.mark.integration

PREFIX = "e4-"


@pytest.fixture()
def clean(db_session):
    def _purge():
        ids = [o.id for o in db_session.execute(select(Organization).where(Organization.slug.like(PREFIX + "%"))).scalars()]
        if ids:
            p = {"ids": ids}
            db_session.execute(text("DELETE FROM question_pools WHERE owner_org_id = ANY(:ids)"), p)
            tests_of = "SELECT test_id FROM tests WHERE organization_id = ANY(:ids)"
            runs_of = "SELECT run_id FROM runs WHERE organization_id = ANY(:ids)"
            for tbl in ("run_evaluations", "run_results"):
                db_session.execute(text(f"DELETE FROM {tbl} WHERE run_id IN ({runs_of}) OR test_id IN ({tests_of})"), p)
            db_session.execute(text("DELETE FROM runs WHERE organization_id = ANY(:ids)"), p)
            db_session.execute(text(f"DELETE FROM test_themes WHERE test_id IN ({tests_of})"), p)
            db_session.execute(text(f"DELETE FROM test_ground_truth WHERE test_id IN ({tests_of})"), p)
            db_session.execute(text("DELETE FROM job_logs WHERE job_id IN (SELECT id FROM jobs WHERE organization_id = ANY(:ids))"), p)
            for tbl, col in (("jobs", "organization_id"), ("scheduled_runs", "organization_id"), ("tests", "organization_id"),
                             ("perimeters", "organization_id"), ("budgets", "organization_id"), ("api_tokens", "organization_id"),
                             ("memberships", "org_id"), ("audit_log", "org_id")):
                db_session.execute(text(f"DELETE FROM {tbl} WHERE {col} = ANY(:ids)"), p)
            db_session.execute(text("UPDATE organizations SET parent_id = NULL WHERE id = ANY(:ids)"), p)
            db_session.execute(text("DELETE FROM organizations WHERE id = ANY(:ids)"), p)
        db_session.execute(text("DELETE FROM themes WHERE slug LIKE :p"), {"p": PREFIX + "%"})
        db_session.commit()
    _purge()
    yield
    _purge()


def _mk(db, slug, parent=None, name=None):
    return tenancy.create_org(db, name=name or slug, slug=PREFIX + slug, parent=parent)


def _peri(db, org, slug):
    p = Perimeter(organization_id=org.id, name=f"Site {slug}", slug=slug, kind="site")
    db.add(p)
    db.commit()
    return p


def _q(db, org, peri, prompt, expected="R"):
    return services.create_test(db, org.id, perimeter_id=peri.id, prompt=prompt, expected_answer=expected,
                                response_quality_prompt_id=None, citation_quality_prompt_id=None)


@pytest.fixture()
def monde(db_session, clean):
    """Ministère > direction > service, plus une entité sans lien. Chaque entité a un site et des questions."""
    m = _mk(db_session, "min", name="Ministère E4")
    d = _mk(db_session, "dir", parent=m, name="Direction E4")
    s = _mk(db_session, "svc", parent=d, name="Service E4")
    other = _mk(db_session, "autre", name="Autre E4")
    sites = {k: _peri(db_session, o, f"site-{k}") for k, o in dict(m=m, d=d, s=s, other=other).items()}
    q = dict(
        m1=_q(db_session, m, sites["m"], "Q ministère 1 ?"),
        m2=_q(db_session, m, sites["m"], "Q ministère 2 ?"),
        d1=_q(db_session, d, sites["d"], "Q direction 1 ?"),
        s1=_q(db_session, s, sites["s"], "Q service 1 ?"),
        o1=_q(db_session, other, sites["other"], "Q autre 1 ?"),
    )
    return dict(m=m, d=d, s=s, other=other, sites=sites, q=q)


def _ids(*tests):
    return {t.test_id for t in tests}


# ---------------------------------------------------------------------
# Visibilité, contenu, composition
# ---------------------------------------------------------------------
def test_visibilite_selon_l_arbre(db_session, monde):
    m, d, s, other = monde["m"], monde["d"], monde["s"], monde["other"]
    priv = pools.create(db_session, m, name="privé", visibility="private")
    desc = pools.create(db_session, m, name="descendants", visibility="descendants")
    tous = pools.create(db_session, d, name="tous", visibility="all")
    vis = {k: {p.id for p, _ in pools.list_visible(db_session, o)} for k, o in dict(m=m, d=d, s=s, other=other).items()}
    assert {priv.id, desc.id, tous.id} <= vis["m"]
    assert desc.id in vis["s"] and tous.id in vis["s"] and priv.id not in vis["s"]
    assert tous.id in vis["other"] and desc.id not in vis["other"] and priv.id not in vis["other"]
    # Les siens d'abord.
    first_pool, first_owner = pools.list_visible(db_session, d)[0]
    assert first_owner.id == d.id
    with pytest.raises(pools.PoolError) as ei:
        pools.get_visible(db_session, other, priv.id)
    assert ei.value.status == 404


def test_nom_unique_par_entite(db_session, monde):
    pools.create(db_session, monde["m"], name="socle")
    with pytest.raises(pools.PoolError) as ei:
        pools.create(db_session, monde["m"], name="socle")
    assert ei.value.status == 409
    pools.create(db_session, monde["d"], name="socle")  # autre entité : permis


def test_un_pool_ne_contient_que_des_questions_du_proprietaire(db_session, monde):
    pool = pools.create(db_session, monde["m"], name="socle")
    assert pools.add_tests(db_session, pool, [monde["q"]["m1"].test_id, monde["q"]["m2"].test_id]) == 2
    assert pools.add_tests(db_session, pool, [monde["q"]["m1"].test_id]) == 0, "idempotent"
    with pytest.raises(pools.PoolError) as ei:
        pools.add_tests(db_session, pool, [monde["q"]["d1"].test_id])
    assert ei.value.status == 400
    pools.remove_test(db_session, pool, monde["q"]["m2"].test_id)
    assert pools.pool_test_ids(db_session, pool.id) == [monde["q"]["m1"].test_id]


def test_inclusions_soi_meme_cycle_invisible(db_session, monde):
    a = pools.create(db_session, monde["m"], name="A", visibility="all")
    b = pools.create(db_session, monde["m"], name="B", visibility="all")
    c = pools.create(db_session, monde["m"], name="C", visibility="all")
    secret = pools.create(db_session, monde["other"], name="secret", visibility="private")
    pools.include(db_session, a, b)
    pools.include(db_session, b, c)
    for parent, child, status in ((a, a, 400), (c, a, 409), (a, secret, 404)):
        with pytest.raises(pools.PoolError) as ei:
            pools.include(db_session, parent, child)
        assert ei.value.status == status
    pools.exclude(db_session, b, c.id)
    pools.include(db_session, c, a)  # plus de cycle une fois B → C retiré


def test_la_composition_n_elargit_jamais_la_visibilite(db_session, monde):
    """Un pool « tous » de la direction inclut un pool « privé » : une autre entité
    n'en reçoit que la partie publique."""
    d, q = monde["d"], monde["q"]
    d2 = _q(db_session, d, monde["sites"]["d"], "Q direction confidentielle ?")
    public = pools.create(db_session, d, name="public", visibility="all")
    prive = pools.create(db_session, d, name="privé", visibility="private")
    pools.add_tests(db_session, public, [q["d1"].test_id])
    pools.add_tests(db_session, prive, [d2.test_id])
    pools.include(db_session, public, prive)
    vu_par_autre = pools.resolve_pool_tests(db_session, public.id, monde["other"])
    assert vu_par_autre.test_ids == {q["d1"].test_id} and vu_par_autre.skipped_pool_ids == {prive.id}
    vu_par_d = pools.resolve_pool_tests(db_session, public.id, d)
    assert vu_par_d.test_ids == {q["d1"].test_id, d2.test_id}
    assert vu_par_d.origins[d2.test_id] == ["privé"]


# ---------------------------------------------------------------------
# Abonnements et questions effectives
# ---------------------------------------------------------------------
def test_abonnement_et_questions_effectives(db_session, monde):
    m, q, site_s = monde["m"], monde["q"], monde["sites"]["s"]
    socle = pools.create(db_session, m, name="socle", visibility="descendants")
    pools.add_tests(db_session, socle, [q["m1"].test_id, q["m2"].test_id])
    pools.subscribe(db_session, site_s, socle.id)
    pools.subscribe(db_session, site_s, socle.id)  # idempotent
    eff = pools.effective_tests(db_session, site_s)
    assert {t.test_id for t in eff} == _ids(q["s1"], q["m1"], q["m2"])
    assert [t.test_id for t in eff] == sorted(t.test_id for t in eff)
    assert pools.origins_for(db_session, site_s) == {q["m1"].test_id: ["socle"], q["m2"].test_id: ["socle"]}
    [sub] = pools.subscriptions(db_session, site_s)
    assert sub.visible and sub.n_questions == 2
    # Sous-sélection et filtre « actif ».
    assert {t.test_id for t in pools.effective_tests(db_session, site_s, test_ids=[q["m2"].test_id])} == _ids(q["m2"])
    services.deactivate_test(db_session, m.id, q["m2"].test_id)
    assert {t.test_id for t in pools.effective_tests(db_session, site_s)} == _ids(q["s1"], q["m1"])
    # Une entité hors sous-arbre ne peut pas s'abonner.
    with pytest.raises(pools.PoolError) as ei:
        pools.subscribe(db_session, monde["sites"]["other"], socle.id)
    assert ei.value.status == 404


def test_visibilite_reduite_desactive_l_abonnement(db_session, monde):
    m, q, site_s = monde["m"], monde["q"], monde["sites"]["s"]
    socle = pools.create(db_session, m, name="socle", visibility="descendants")
    pools.add_tests(db_session, socle, [q["m1"].test_id])
    pools.subscribe(db_session, site_s, socle.id)
    pools.update(db_session, socle, visibility="private")
    [sub] = pools.subscriptions(db_session, site_s)
    assert not sub.visible and sub.n_questions == 0
    assert {t.test_id for t in pools.effective_tests(db_session, site_s)} == _ids(q["s1"])


def test_suppression_du_pool(db_session, monde):
    m, q, site_s = monde["m"], monde["q"], monde["sites"]["s"]
    socle = pools.create(db_session, m, name="socle", visibility="all")
    parent = pools.create(db_session, m, name="parent", visibility="all")
    pools.add_tests(db_session, socle, [q["m1"].test_id])
    pools.include(db_session, parent, socle)
    pools.subscribe(db_session, site_s, socle.id)
    assert pools.usage(db_session, socle) == {"subscriptions": 1, "included_by": 1}
    assert pools.delete(db_session, socle) == {"subscriptions": 1, "included_by": 1}
    assert pools.subscriptions(db_session, site_s) == [] and pools.included_ids(db_session, parent.id) == []
    assert services.get_test(db_session, m.id, q["m1"].test_id) is not None, "la question survit au pool"


# ---------------------------------------------------------------------
# Lancement, devis, worker
# ---------------------------------------------------------------------
@pytest.fixture()
def abonne(db_session, monde):
    socle = pools.create(db_session, monde["m"], name="socle", visibility="descendants")
    pools.add_tests(db_session, socle, [monde["q"]["m1"].test_id])
    pools.subscribe(db_session, monde["sites"]["s"], socle.id)
    return socle


def _models(db):
    tested = db.execute(select(Model).where(Model.model_name == "openrouter", Model.is_active.is_(True)).order_by(Model.model_id)).scalars().first()
    judge = db.execute(select(Model).where(Model.model_name == "albert", Model.is_judge.is_(True)).order_by(Model.model_id)).scalars().first()
    return tested, judge


def test_lancement_accepte_les_questions_des_pools(db_session, monde, abonne):
    tested, judge = _models(db_session)
    s, q = monde["s"], monde["q"]
    args = dict(perimeter_id=monde["sites"]["s"].id, tested_models=[tested.model_version],
                judge_models=[judge.model_version], repeats=1, role=None, is_platform_admin=True)
    p = launching.validate_selection(db_session, s.id, test_ids=[q["s1"].test_id, q["m1"].test_id], **args)
    assert p["test_ids"] is None, "toutes les questions effectives ⇒ pas de sous-sélection"
    p = launching.validate_selection(db_session, s.id, test_ids=[q["m1"].test_id], **args)
    assert p["test_ids"] == [q["m1"].test_id]
    with pytest.raises(launching.LaunchError) as ei:
        launching.validate_selection(db_session, s.id, test_ids=[q["m2"].test_id], **args)
    assert "hors du périmètre" in ei.value.detail, "question du ministère non partagée"


def test_devis_et_worker_portent_sur_les_questions_effectives(db_session, monde, abonne, monkeypatch):
    s, q, site_s = monde["s"], monde["q"], monde["sites"]["s"]
    seen = {}

    def fake_estimate(session, *, org_id, tests, tested_models, judges):
        seen["ids"] = {t.test_id for t in tests}
        from decimal import Decimal
        return {"total_eur": Decimal("0")}

    monkeypatch.setattr(pricing, "estimate_scan_cost", fake_estimate)
    params = dict(tested_models=["x"], judges=[], test_ids=None)
    launching.estimate_and_check_budget(db_session, s.id, params, perimeter_id=site_s.id)
    assert seen["ids"] == _ids(q["s1"], q["m1"])
    launching.estimate_and_check_budget(db_session, s.id, params)
    assert seen["ids"] == _ids(q["s1"]), "sans périmètre : questions propres de l'entité seulement"

    got = jobs.select_tests(db_session, organization_id=s.id, perimeter_id=site_s.id, test_ids=None)
    assert {t.test_id for t in got} == _ids(q["s1"], q["m1"])
    got = jobs.select_tests(db_session, organization_id=s.id, perimeter_id=site_s.id, test_ids=[q["m1"].test_id])
    assert {t.test_id for t in got} == _ids(q["m1"])
    assert jobs.select_tests(db_session, organization_id=monde["other"].id, perimeter_id=site_s.id, test_ids=None) == [], \
        "un périmètre d'une autre entité ne fournit rien"


def test_stats_par_question_incluent_les_questions_de_pool(db_session, monde, abonne):
    """Une question de pool appartient au ministère, mais ses évaluations dans les runs
    du service comptent dans les statistiques du service (et pas dans celles du ministère)."""
    s, q = monde["s"], monde["q"]
    tested, judge = _models(db_session)
    run = RunRow(organization_id=s.id, perimeter_id=monde["sites"]["s"].id, tested_model_id=tested.model_id)
    db_session.add(run)
    db_session.commit()
    db_session.add_all([
        RunEvaluation(run_id=run.run_id, test_id=q["m1"].test_id, judge_model_id=judge.model_id, judge_run_index=1,
                      response_quality_score=4, citation_quality_score=2),
        RunEvaluation(run_id=run.run_id, test_id=q["s1"].test_id, judge_model_id=judge.model_id, judge_run_index=1,
                      response_quality_score=8, citation_quality_score=6),
    ])
    db_session.commit()
    stats = {r["test_id"]: r for r in services.question_stats(db_session, s.id)}
    assert set(stats) == _ids(q["m1"], q["s1"])
    assert stats[q["m1"].test_id]["avg_response"] == pytest.approx(4.0)
    assert services.question_stats(db_session, monde["m"].id) == []


def test_part_des_citations_officielles(client, db_session, monde):
    s, q, site_s = monde["s"], monde["q"], monde["sites"]["s"]
    tested, _ = _models(db_session)
    site_s.domains = ["service-public.fr"]
    run = RunRow(organization_id=s.id, perimeter_id=site_s.id, tested_model_id=tested.model_id)
    db_session.add(run)
    db_session.commit()
    db_session.add(RunResult(run_id=run.run_id, test_id=q["s1"].test_id, raw_answer="R", raw_citations=[
        "https://www.service-public.fr/a", {"url": "https://entreprendre.service-public.fr/b"},
        "https://wikipedia.org/c", "https://faux-service-public.fr/d",
    ]))
    db_session.commit()
    detail = services.get_run_detail(db_session, s.id, run.run_id)
    assert detail["official_share"] == pytest.approx(0.5) and detail["official_domains"] == ["service-public.fr"]
    assert detail["results"][0]["official_citations"] == {
        "https://www.service-public.fr/a", "https://entreprendre.service-public.fr/b"}
    page = client.get(f"/o/{s.slug}/runs/{run.run_id}")
    assert page.status_code == 200 and "Sources officielles : 50 %" in page.text and page.text.count(">officielle<") == 2
    body = client.get(f"/api/v1/orgs/{s.slug}/runs/{run.run_id}", headers=_bearer(db_session, s, "viewer")).json()
    assert body["official_share"] == pytest.approx(0.5) and body["results"][0]["official_share"] == pytest.approx(0.5)


# ---------------------------------------------------------------------
# Thèmes
# ---------------------------------------------------------------------
def test_catalogue_de_themes(db_session, monde):
    q, site_s = monde["q"], monde["sites"]["s"]
    energie = themes.create(db_session, label="Énergie", slug=PREFIX + "energie")
    sante = themes.create(db_session, label="Santé", slug=PREFIX + "sante")
    with pytest.raises(themes.ThemeError) as ei:
        themes.create(db_session, label="Énergie bis", slug=PREFIX + "energie")
    assert ei.value.status == 409
    themes.set_for_test(db_session, q["s1"].test_id, [energie.id, sante.id, energie.id])
    themes.set_for_test(db_session, q["m1"].test_id, [sante.id])
    themes.set_for_perimeter(db_session, site_s.id, [energie.id])
    assert themes.test_ids_with_theme(db_session, sante.id) == _ids(q["s1"], q["m1"])
    assert {t.slug for t in themes.for_tests(db_session, [q["s1"].test_id])[q["s1"].test_id]} == {energie.slug, sante.slug}
    assert [t.id for t in themes.for_perimeter(db_session, site_s.id)] == [energie.id]
    with pytest.raises(themes.ThemeError):
        themes.set_for_test(db_session, q["s1"].test_id, [999_999])
    assert themes.usage(db_session, energie.id) == {"questions": 1, "perimeters": 1}
    assert themes.delete(db_session, energie) == {"questions": 1, "perimeters": 1}
    assert themes.for_perimeter(db_session, site_s.id) == []


# ---------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------
def test_ui_pools_abonnement_et_themes(client, db_session, monde):
    m, s, q = monde["m"], monde["s"], monde["q"]
    r = client.post(f"/o/{m.slug}/pools/new", data={"name": "Socle UI", "description": "commun", "visibility": "descendants"})
    assert r.status_code == 303
    pool_id = int(r.headers["location"].rsplit("/", 1)[1])
    assert client.post(f"/o/{m.slug}/pools/{pool_id}/tests", data={"test_ids": [q["m1"].test_id]}).status_code == 303
    page = client.get(f"/o/{m.slug}/pools/{pool_id}")
    assert page.status_code == 200 and "Q ministère 1 ?" in page.text and "Ajouter des questions" in page.text
    # Le service voit le pool en lecture seule et l'abonne à son site.
    page = client.get(f"/o/{s.slug}/pools")
    assert page.status_code == 200 and "Socle UI" in page.text
    assert "lecture seule" in client.get(f"/o/{s.slug}/pools/{pool_id}").text
    site_s = monde["sites"]["s"]
    assert client.post(f"/o/{s.slug}/perimeters/{site_s.id}/pools", data={"pool_id": pool_id}).status_code == 303
    page = client.get(f"/o/{s.slug}/perimeters/{site_s.id}")
    assert "Questions issues des pools (1)" in page.text and "Q ministère 1 ?" in page.text
    # Le formulaire de lancement propose la question du pool.
    assert "Q ministère 1 ?" in client.get(f"/o/{s.slug}/launch?perimeter_id={site_s.id}").text
    # Une entité hors sous-arbre : 404.
    assert client.get(f"/o/{monde['other'].slug}/pools/{pool_id}").status_code == 404
    # Domaines et thèmes du périmètre.
    th = themes.create(db_session, label="Fiscalité", slug=PREFIX + "fiscalite")
    r = client.post(f"/o/{s.slug}/perimeters/{site_s.id}/edit", data={
        "name": site_s.name, "domains": "https://www.impots.gouv.fr/x\nservice-public.fr", "theme_ids": [th.id],
    })
    assert r.status_code == 303
    db_session.expire_all()
    assert db_session.get(Perimeter, site_s.id).domains == ["impots.gouv.fr", "service-public.fr"]
    assert "Fiscalité" in client.get(f"/o/{s.slug}/perimeters/{site_s.id}").text
    assert client.post(f"/o/{s.slug}/perimeters/{site_s.id}/edit", data={"name": "x", "domains": "pas un domaine"}).status_code == 400
    # Thème d'une question et filtre de la liste.
    r = client.post(f"/o/{s.slug}/tests/{q['s1'].test_id}/edit", data={
        "perimeter_id": site_s.id, "prompt": "Q service 1 ?", "expected_answer": "R", "theme_ids": [th.id],
    })
    assert r.status_code == 303
    page = client.get(f"/o/{s.slug}/tests?theme={th.slug}")
    assert page.status_code == 200 and "Q service 1 ?" in page.text
    # Désabonnement.
    assert client.post(f"/o/{s.slug}/perimeters/{site_s.id}/pools/{pool_id}/unsubscribe").status_code == 303
    assert "Questions issues des pools" not in client.get(f"/o/{s.slug}/perimeters/{site_s.id}").text


def test_ui_admin_themes(client, db_session, clean):
    r = client.post("/admin/themes/new", data={"label": "Transport E4", "slug": PREFIX + "transport"})
    assert r.status_code == 303
    page = client.get("/admin/themes")
    assert page.status_code == 200 and "Transport E4" in page.text
    th = db_session.execute(text("SELECT id FROM themes WHERE slug = :s"), {"s": PREFIX + "transport"}).scalar_one()
    assert client.post(f"/admin/themes/{th}/rename", data={"label": "Mobilités E4"}).status_code == 303
    assert "Mobilités E4" in client.get("/admin/themes").text
    assert client.post("/admin/themes/new", data={"label": "x", "slug": PREFIX + "transport"}).status_code == 409
    assert client.post(f"/admin/themes/{th}/delete").status_code == 303


# ---------------------------------------------------------------------
# API v1
# ---------------------------------------------------------------------
def _bearer(db, org, role):
    _, plain = api_tokens.create(db, org_id=org.id, name=f"tok-{role}", role=role, created_by=None)
    return {"Authorization": f"Bearer {plain}"}


def test_api_pools_abonnements_questions_effectives(client, db_session, monde):
    m, s, q = monde["m"], monde["s"], monde["q"]
    h_m, h_s = _bearer(db_session, m, "editor"), _bearer(db_session, s, "editor")
    h_view = _bearer(db_session, s, "viewer")
    base_m, base_s = f"/api/v1/orgs/{m.slug}", f"/api/v1/orgs/{s.slug}"

    r = client.post(f"{base_m}/pools", json={"name": "Socle API", "visibility": "descendants"}, headers=h_m)
    assert r.status_code == 201, r.text
    pool = r.json()
    assert pool["owner_org_slug"] == m.slug and pool["test_ids"] == []
    assert client.post(f"{base_m}/pools", json={"name": "Socle API"}, headers=h_m).status_code == 409
    assert client.post(f"{base_s}/pools", json={"name": "v"}, headers=h_view).status_code == 403
    r = client.post(f"{base_m}/pools/{pool['id']}/questions", json={"test_ids": [q["m1"].test_id]}, headers=h_m)
    assert r.status_code == 200 and r.json()["test_ids"] == [q["m1"].test_id] and r.json()["n_questions"] == 1
    r = client.post(f"{base_m}/pools/{pool['id']}/questions", json={"test_ids": [q["d1"].test_id]}, headers=h_m)
    assert r.status_code == 400 and r.headers["content-type"].startswith("application/problem+json")

    # Le service le voit, ne peut pas le modifier, l'abonne à son site.
    assert pool["id"] in {p["id"] for p in client.get(f"{base_s}/pools", headers=h_view).json()}
    assert client.patch(f"{base_s}/pools/{pool['id']}", json={"name": "pirate"}, headers=h_s).status_code == 404
    site_s = monde["sites"]["s"]
    r = client.post(f"{base_s}/perimeters/{site_s.id}/pools", json={"pool_id": pool["id"]}, headers=h_s)
    assert r.status_code == 201 and r.json()[0]["visible"] and r.json()[0]["n_questions"] == 1
    eff = client.get(f"{base_s}/perimeters/{site_s.id}/effective-questions", headers=h_view).json()
    by_id = {e["test_id"]: e for e in eff}
    assert set(by_id) == _ids(q["s1"], q["m1"])
    assert by_id[q["s1"].test_id]["own"] and not by_id[q["m1"].test_id]["own"]
    assert by_id[q["m1"].test_id]["pools"] == ["Socle API"] and by_id[q["m1"].test_id]["organization_id"] == m.id
    peri = client.get(f"{base_s}/perimeters/{site_s.id}", headers=h_view).json()
    assert peri["n_questions"] == 1 and peri["n_pooled_questions"] == 1

    # Inclusion avec cycle refusée ; visibilité réduite → abonnement inactif.
    other_pool = client.post(f"{base_m}/pools", json={"name": "Autre", "visibility": "all"}, headers=h_m).json()
    assert client.post(f"{base_m}/pools/{pool['id']}/includes", json={"pool_id": other_pool["id"]}, headers=h_m).status_code == 200
    assert client.post(f"{base_m}/pools/{other_pool['id']}/includes", json={"pool_id": pool["id"]}, headers=h_m).status_code == 409
    r = client.patch(f"{base_m}/pools/{pool['id']}", json={"visibility": "private"}, headers=h_m)
    assert r.status_code == 200 and r.json()["visibility"] == "private"
    subs = client.get(f"{base_s}/perimeters/{site_s.id}/pools", headers=h_view).json()
    assert subs[0]["visible"] is False and subs[0]["n_questions"] == 0
    assert client.get(f"{base_s}/pools/{pool['id']}", headers=h_view).status_code == 404
    assert client.patch(f"{base_m}/pools/{pool['id']}", json={"visibility": "public"}, headers=h_m).status_code == 422

    assert client.delete(f"{base_s}/perimeters/{site_s.id}/pools/{pool['id']}", headers=h_s).status_code == 204
    assert client.delete(f"{base_m}/pools/{pool['id']}", headers=h_m).status_code == 204
    assert client.get(f"{base_m}/pools/{pool['id']}", headers=h_m).status_code == 404


def test_api_themes_domaines(client, db_session, monde):
    s, q = monde["s"], monde["q"]
    h_s = _bearer(db_session, s, "editor")
    # Catalogue : lecture par jeton, écriture réservée à l'admin plateforme (session).
    assert client.post("/api/v1/themes", json={"label": "Interdit"}, headers=h_s).status_code == 403
    r = client.post("/api/v1/themes", json={"label": "Logement E4", "slug": PREFIX + "logement"})
    assert r.status_code == 201, r.text
    th = r.json()
    assert th in client.get("/api/v1/themes", headers=h_s).json()
    assert client.patch(f"/api/v1/themes/{th['id']}", json={"label": "Habitat E4"}).json()["label"] == "Habitat E4"

    base = f"/api/v1/orgs/{s.slug}"
    r = client.post(f"{base}/perimeters", json={
        "name": "Site API", "slug": "site-api", "domains": ["https://www.anil.org/x", "ANIL.org", "service-public.fr"],
        "theme_ids": [th["id"]],
    }, headers=h_s)
    assert r.status_code == 201, r.text
    assert r.json()["domains"] == ["anil.org", "service-public.fr"] and r.json()["theme_ids"] == [th["id"]]
    pid = r.json()["id"]
    r = client.patch(f"{base}/perimeters/{pid}", json={"domains": [], "theme_ids": []}, headers=h_s)
    assert r.json()["domains"] == [] and r.json()["theme_ids"] == []
    assert client.patch(f"{base}/perimeters/{pid}", json={"domains": ["pas un domaine"]}, headers=h_s).status_code == 400
    assert client.patch(f"{base}/perimeters/{pid}", json={"theme_ids": [999_999]}, headers=h_s).status_code == 400

    r = client.post(f"{base}/questions", json={"perimeter_id": pid, "prompt": "Q API ?", "theme_ids": [th["id"]]}, headers=h_s)
    assert r.status_code == 201 and r.json()["theme_ids"] == [th["id"]]
    listed = client.get(f"{base}/questions", params={"theme_id": th["id"]}, headers=h_s).json()
    assert [i["test_id"] for i in listed["items"]] == [r.json()["test_id"]]
    r = client.patch(f"{base}/questions/{q['s1'].test_id}", json={"theme_ids": [th["id"]]}, headers=h_s)
    assert r.status_code == 200 and r.json()["theme_ids"] == [th["id"]]
    assert client.post(f"{base}/questions", json={"perimeter_id": pid, "prompt": "x", "theme_ids": [999_999]}, headers=h_s).status_code == 400

    assert client.delete(f"/api/v1/themes/{th['id']}", headers=h_s).status_code == 403
    assert client.delete(f"/api/v1/themes/{th['id']}").status_code == 204
    assert client.get(f"{base}/questions/{q['s1'].test_id}", headers=h_s).json()["theme_ids"] == []
