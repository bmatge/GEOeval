"""Suite E7 sur PostgreSQL : signalements, récapitulatif quotidien, chute des citations officielles."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from geoeval.db.models import (
    Campaign,
    CampaignParticipant,
    Model,
    Notification,
    Organization,
    Perimeter,
    RunResult,
    RunRow,
    User,
)
from geoeval.web import api_tokens, detectors, mailer, notifications, pools, reports, services, tenancy
from tests.conftest import TEST_USER_EMAIL

pytestmark = pytest.mark.integration

PREFIX = "e7s-"
DOMAIN = "@e7s.test"


@pytest.fixture()
def clean(db_session):
    def _purge():
        ids = [o.id for o in db_session.execute(select(Organization).where(Organization.slug.like(PREFIX + "%"))).scalars()]
        p = {"ids": ids or [-1], "d": "%" + DOMAIN}
        tests_of = "SELECT test_id FROM tests WHERE organization_id = ANY(:ids)"
        runs_of = "SELECT run_id FROM runs WHERE organization_id = ANY(:ids)"
        db_session.execute(text(f"DELETE FROM test_reports WHERE owner_org_id = ANY(:ids) OR reporter_org_id = ANY(:ids) "
                                f"OR test_id IN ({tests_of})"), p)
        db_session.execute(text(f"DELETE FROM run_evaluations WHERE run_id IN ({runs_of}) OR test_id IN ({tests_of})"), p)
        db_session.execute(text(f"DELETE FROM run_results WHERE run_id IN ({runs_of}) OR test_id IN ({tests_of})"), p)
        db_session.execute(text("DELETE FROM usage WHERE organization_id = ANY(:ids)"), p)
        db_session.execute(text(f"DELETE FROM runs WHERE run_id IN ({runs_of})"), p)
        db_session.execute(text("DELETE FROM notifications WHERE organization_id = ANY(:ids) "
                                "OR user_id IN (SELECT id FROM users WHERE email LIKE :d)"), p)
        db_session.execute(text("DELETE FROM campaigns WHERE owner_org_id = ANY(:ids)"), p)
        db_session.execute(text("DELETE FROM question_pools WHERE owner_org_id = ANY(:ids)"), p)
        db_session.execute(text(f"DELETE FROM test_ground_truth WHERE test_id IN ({tests_of})"), p)
        for tbl, col in (("detector_settings", "organization_id"), ("tests", "organization_id"),
                         ("perimeters", "organization_id"), ("api_tokens", "organization_id"),
                         ("memberships", "org_id"), ("audit_log", "org_id")):
            db_session.execute(text(f"DELETE FROM {tbl} WHERE {col} = ANY(:ids)"), p)
        db_session.execute(text("UPDATE organizations SET parent_id = NULL WHERE id = ANY(:ids)"), p)
        db_session.execute(text("DELETE FROM organizations WHERE id = ANY(:ids)"), p)
        db_session.execute(text("DELETE FROM notification_preferences WHERE user_id IN (SELECT id FROM users WHERE email LIKE :d)"), p)
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


def _perimeter(db, org, slug="site", domains=None):
    peri = Perimeter(organization_id=org.id, name=f"Site {slug}", slug=slug, kind="site", domains=domains or [])
    db.add(peri)
    db.commit()
    return peri


def _question(db, org, peri, prompt="Quel est le délai légal ?"):
    return services.create_test(db, org.id, perimeter_id=peri.id, prompt=prompt, expected_answer="Deux mois",
                                response_quality_prompt_id=None, citation_quality_prompt_id=None)


def _tested(db):
    return db.execute(select(Model).where(Model.model_name == "openrouter").order_by(Model.model_id)).scalars().first()


def _run(db, org, tests, tested, *, perimeter=None, citations=None):
    r = RunRow(organization_id=org.id, tested_model_id=tested.model_id,
               perimeter_id=perimeter.id if perimeter is not None else None)
    db.add(r)
    db.commit()
    for t in tests:
        db.add(RunResult(run_id=r.run_id, test_id=t.test_id, raw_answer="…", raw_citations=citations or []))
    db.commit()
    return r.run_id


@pytest.fixture()
def monde(db_session, clean):
    """Producteur (propriétaire des questions, partage un pool public) et consommateur."""
    prod = tenancy.create_org(db_session, name="Producteur E7s", slug=PREFIX + "prod")
    cons = tenancy.create_org(db_session, name="Consommateur E7s", slug=PREFIX + "cons")
    tiers = tenancy.create_org(db_session, name="Tiers E7s", slug=PREFIX + "tiers")
    peri = _perimeter(db_session, prod)
    t = _question(db_session, prod, peri)
    return dict(prod=prod, cons=cons, tiers=tiers, peri=peri, t=t,
                editor_p=_user(db_session, "editor-p", prod, "editor"),
                viewer_p=_user(db_session, "viewer-p", prod, "viewer"),
                viewer_c=_user(db_session, "viewer-c", cons, "viewer"),
                editor_c=_user(db_session, "editor-c", cons, "editor"))


@pytest.fixture()
def smtp(monkeypatch):
    sent = []

    class Fake:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, *a, **k):
            pass

        def login(self, *a, **k):
            pass

        def send_message(self, msg):
            sent.append(msg)

    monkeypatch.setattr(mailer.smtplib, "SMTP", Fake)
    monkeypatch.setenv("GEOEVAL_SMTP_HOST", "smtp.test")
    return sent


def _kinds(db, user, kind):
    return [n for n in notifications.list_for_user(db, user.id) if n.kind == kind]


# ---------------------------------------------------------------------
# Signalements : visibilité
# ---------------------------------------------------------------------
def test_visibilite_propre_pool_campagne_run(db_session, monde):
    prod, cons, tiers, t = monde["prod"], monde["cons"], monde["tiers"], monde["t"]
    assert reports.can_see_test(db_session, prod, t), "propriétaire"
    assert not reports.can_see_test(db_session, cons, t), "ni pool, ni campagne, ni run"
    # Pool partagé par référence, abonné à un périmètre du consommateur.
    pool = pools.create(db_session, prod, name="Pool E7s", visibility="all")
    pools.add_tests(db_session, pool, [t.test_id])
    pools.subscribe(db_session, _perimeter(db_session, cons, "conso"), pool.id)
    assert reports.can_see_test(db_session, cons, t)
    # Campagne : seulement une fois activée (protocole figé), pas en brouillon.
    c = Campaign(owner_org_id=prod.id, name="Camp E7s", status="draft", schedule_kind="once", schedule_config={},
                 protocol={"test_ids": [t.test_id]})
    db_session.add(c)
    db_session.commit()
    db_session.add(CampaignParticipant(campaign_id=c.id, organization_id=tiers.id))
    db_session.commit()
    assert not reports.can_see_test(db_session, tiers, t)
    c.status = "active"
    db_session.commit()
    assert reports.can_see_test(db_session, tiers, t)
    # Run : une question apparue dans un run de l'entité reste signalable.
    other = tenancy.create_org(db_session, name="Autre E7s", slug=PREFIX + "autre")
    assert not reports.can_see_test(db_session, other, t)
    _run(db_session, other, [t], _tested(db_session))
    assert reports.can_see_test(db_session, other, t)


# ---------------------------------------------------------------------
# Signalements : cycle complet et notifications
# ---------------------------------------------------------------------
def test_signaler_puis_traiter(db_session, monde):
    prod, cons, t = monde["prod"], monde["cons"], monde["t"]
    run_id = _run(db_session, cons, [t], _tested(db_session))
    with pytest.raises(reports.ReportError) as e:
        reports.create(db_session, monde["tiers"], test_id=t.test_id, category="obsolete", comment="",
                       reporter_user_id=None)
    assert e.value.status == 404, "question invisible = introuvable"
    for bad in (dict(category="spam", comment="x"), dict(category="other", comment="  ")):
        with pytest.raises(reports.ReportError):
            reports.create(db_session, cons, test_id=t.test_id, reporter_user_id=None, **bad)
    other_run = _run(db_session, prod, [t], _tested(db_session))
    with pytest.raises(reports.ReportError):
        reports.create(db_session, cons, test_id=t.test_id, category="obsolete", comment="", reporter_user_id=None,
                       run_id=other_run)
    r = reports.create(db_session, cons, test_id=t.test_id, category="expected_answer", comment="Le délai est de 3 mois.",
                       reporter_user_id=monde["viewer_c"].id, run_id=run_id)
    assert (r.owner_org_id, r.reporter_org_id, r.status, r.run_id) == (prod.id, cons.id, "open", run_id)
    [n] = _kinds(db_session, monde["editor_p"], "report_opened")
    assert n.link == f"/o/{prod.slug}/reports/{r.id}" and "Consommateur E7s" in n.title and "3 mois" in n.body
    assert _kinds(db_session, monde["viewer_p"], "report_opened") == [], "editor+ seulement"
    assert reports.open_counts(db_session, [t.test_id]) == {t.test_id: 1}
    assert reports.open_count_for_org(db_session, prod.id) == 1
    assert [x.id for x in reports.list_received(db_session, prod)] == [r.id]
    assert [x.id for x in reports.list_sent(db_session, cons)] == [r.id]
    assert reports.get_for_org(db_session, cons, r.id) == (r, False)
    with pytest.raises(reports.ReportError):
        reports.get_for_org(db_session, monde["tiers"], r.id)
    with pytest.raises(reports.ReportError):
        reports.resolve(db_session, r, status="fixed", comment="", resolved_by=monde["editor_p"].id)
    with pytest.raises(reports.ReportError):
        reports.resolve(db_session, r, status="open", comment="x", resolved_by=monde["editor_p"].id)
    reports.resolve(db_session, r, status="fixed", comment="Corrigé, merci.", resolved_by=monde["editor_p"].id)
    assert r.status == "fixed" and r.resolved_at is not None and reports.open_counts(db_session, [t.test_id]) == {}
    [back] = _kinds(db_session, monde["viewer_c"], "report_resolved")
    assert back.link == f"/o/{cons.slug}/reports/{r.id}" and "corrigé" in back.title and "Corrigé, merci." in back.body
    assert _kinds(db_session, monde["editor_c"], "report_resolved") == [], "seul l'auteur est prévenu"
    with pytest.raises(reports.ReportError) as e:
        reports.resolve(db_session, r, status="rejected", comment="x", resolved_by=None)
    assert e.value.status == 409


# ---------------------------------------------------------------------
# Récapitulatif quotidien
# ---------------------------------------------------------------------
def test_recapitulatif_quotidien(db_session, monde, smtp):
    prod, editor, viewer = monde["prod"], monde["editor_p"], monde["viewer_p"]
    notifications.set_preferences(db_session, editor.id, {"report_opened": "digest", "job_failed": "digest"})
    e = notifications.notify(db_session, prod, "report_opened", title="Signalement A", link="/o/x/reports/1", dedup_key="dg:a")
    assert e.emailed == [] and smtp == []
    assert {n.user_id: n.email_status for n in e.created}[editor.id] == "digest_pending"
    notifications.notify(db_session, prod, "job_failed", title="Échec B", dedup_key="dg:b")
    lu = notifications.notify(db_session, prod, "report_opened", title="Déjà lu C", dedup_key="dg:c")
    mine_c = next(n for n in lu.created if n.user_id == editor.id)
    notifications.mark_read(db_session, editor.id, mine_c.id)
    mine = lambda: [m for m in smtp if m["To"] == editor.email]  # noqa: E731
    # Échéance non atteinte : rien ne part (notifications créées après la dernière échéance).
    notifications.send_digests(db_session, now=datetime.now(timezone.utc))
    assert mine() == []
    # Le lendemain : un seul email groupé, les notifications lues ne sont pas rappelées.
    tomorrow = datetime.now(timezone.utc) + timedelta(days=1, hours=1)
    assert notifications.send_digests(db_session, now=tomorrow) >= 1
    [msg] = mine()
    assert "2 notification(s)" in msg["Subject"]
    body = msg.get_content()
    assert "Signalement A" in body and "Échec B" in body and "Déjà lu C" not in body
    statuses = {n.title: n.email_status for n in notifications.list_for_user(db_session, editor.id)}
    assert statuses == {"Signalement A": "sent", "Échec B": "sent", "Déjà lu C": "none"}
    assert notifications.send_digests(db_session, now=tomorrow) == 0 and len(mine()) == 1, "idempotent"
    assert _kinds(db_session, viewer, "report_opened") == []
    assert notifications.send_digests_safely(db_session) == 0


def test_preferences_trois_modes_migrent_les_booleens(db_session, monde):
    editor = monde["editor_p"]
    prefs = notifications.set_preferences(db_session, editor.id, {"always_wrong": True, "budget_threshold": False,
                                                                   "citation_drop": "digest"})
    assert (prefs["always_wrong"], prefs["budget_threshold"], prefs["citation_drop"]) == ("immediate", "none", "digest")
    assert notifications.email_enabled(db_session, editor.id, "always_wrong")
    assert not notifications.email_enabled(db_session, editor.id, "citation_drop"), "digest ≠ immédiat"
    with pytest.raises(ValueError):
        notifications.set_preferences(db_session, editor.id, {"always_wrong": "fax"})


# ---------------------------------------------------------------------
# Chute des citations officielles
# ---------------------------------------------------------------------
OFF, EXT = "https://www.service-public.fr/particuliers/vosdroits/F1", "https://blog.exemple.com/article"


def _cit(n_off, n_ext):
    return [OFF] * n_off + [{"url": EXT, "title": "x"}] * n_ext


def test_chute_des_citations_officielles(db_session, monde):
    cons, editor = monde["cons"], monde["editor_c"]
    peri = _perimeter(db_session, cons, "sp", domains=["service-public.fr"])
    t = _question(db_session, cons, peri)
    tested = _tested(db_session)
    alerts = lambda: _kinds(db_session, editor, "citation_drop")  # noqa: E731
    first = [_run(db_session, cons, [t], tested, perimeter=peri, citations=_cit(4, 1)) for _ in range(2)]
    assert detectors.check_citation_drop(db_session, first[-1]) == 0, "moins de 3 runs de référence"
    _run(db_session, cons, [t], tested, perimeter=peri, citations=[])            # sans citation : ignoré
    _run(db_session, cons, [t], tested, perimeter=peri, citations=_cit(3, 2))    # référence : 80, 80, 60 → 73,3 %
    slight = _run(db_session, cons, [t], tested, perimeter=peri, citations=_cit(3, 3))   # 50 %
    assert detectors.check_citation_drop(db_session, slight) == 1               # 73,3 − 50 = 23,3 ≥ 20 ; editor+
    [n] = alerts()
    assert n.link == f"/o/{cons.slug}/runs/{slight}" and n.payload["share"] == 0.5 and "Site sp" in n.title
    assert detectors.check_citation_drop(db_session, slight) == 0, "une alerte par run"
    stable = _run(db_session, cons, [t], tested, perimeter=peri, citations=_cit(3, 3))   # réf 50, 60, 80 → 63,3 ; 50 %
    assert detectors.check_citation_drop(db_session, stable) == 0, "13 points < 20"
    # Réglage hérité plus sensible, posé sur l'entité.
    detectors.set_settings(db_session, cons, runs=None, threshold=None, citation_drop_points="10")
    eff = detectors.effective_settings(db_session, cons)
    assert (eff.citation_drop_points, eff.citation_drop_from.id) == (Decimal("10"), cons.id)
    assert detectors.check_citation_drop(db_session, stable) == 1
    # Hors périmètre doté de domaines, autre IA, run inconnu : rien.
    bare = _perimeter(db_session, cons, "nu")
    assert detectors.check_citation_drop(db_session, _run(db_session, cons, [t], tested, perimeter=bare,
                                                          citations=_cit(0, 5))) == 0
    assert detectors.check_citation_drop(db_session, _run(db_session, cons, [t], tested, citations=_cit(0, 5))) == 0
    assert detectors.check_citation_drop(db_session, 999_999_999) == 0
    for bad in ("0", "101", "x"):
        with pytest.raises(ValueError):
            detectors.set_settings(db_session, cons, runs=None, threshold=None, citation_drop_points=bad)


def test_hook_apres_evaluation_lance_les_deux_detecteurs(db_session, monde, monkeypatch):
    calls = []
    monkeypatch.setattr(detectors, "check_always_wrong", lambda s, r: calls.append(("aw", r)) or 1)

    def boom(s, r):
        raise RuntimeError("panne")
    monkeypatch.setattr(detectors, "check_citation_drop", boom)
    assert detectors.after_evaluation_safely(42) == 1 and calls == [("aw", 42)], "l'échec de l'un n'arrête pas l'autre"


# ---------------------------------------------------------------------
# UI et API
# ---------------------------------------------------------------------
def _ci_user(db):
    return db.execute(select(User).where(User.email == TEST_USER_EMAIL)).scalar_one()


def _bearer(db, org, role):
    _, plain = api_tokens.create(db, org_id=org.id, name=f"tok-{role}", role=role, created_by=None)
    return {"Authorization": f"Bearer {plain}"}


def test_ui_signalements(client, db_session, monde):
    client.get("/")
    me = _ci_user(db_session)
    prod, cons, t = monde["prod"], monde["cons"], monde["t"]
    tenancy.add_membership(db_session, user_id=me.id, org_id=cons.id, role="org_admin")
    run_id = _run(db_session, cons, [t], _tested(db_session))
    page = client.get(f"/o/{cons.slug}/runs/{run_id}")
    assert page.status_code == 200 and f"/o/{cons.slug}/tests/{t.test_id}/report?run_id={run_id}" in page.text
    form = client.get(f"/o/{cons.slug}/tests/{t.test_id}/report", params={"run_id": run_id})
    assert form.status_code == 200 and t.prompt in form.text
    assert client.get(f"/o/{monde['tiers'].slug}/tests/{t.test_id}/report").status_code in (403, 404)
    r = client.post(f"/o/{cons.slug}/tests/{t.test_id}/report",
                    data={"category": "obsolete", "comment": "Loi modifiée en 2026.", "run_id": str(run_id)})
    assert r.status_code == 303
    report = db_session.execute(select(reports.TestReport).where(reports.TestReport.reporter_org_id == cons.id)).scalar_one()
    assert r.headers["location"] == f"/o/{cons.slug}/reports/{report.id}"
    assert client.post(f"/o/{cons.slug}/tests/{t.test_id}/report", data={"category": "obsolete", "run_id": "x"}).status_code == 400
    sent = client.get(f"/o/{cons.slug}/reports", params={"tab": "sent"})
    assert sent.status_code == 200 and "Loi modifiée en 2026." in sent.text
    detail = client.get(f"/o/{cons.slug}/reports/{report.id}")
    assert detail.status_code == 200 and "/resolve" not in detail.text, "l'auteur ne traite pas"
    assert client.post(f"/o/{cons.slug}/reports/{report.id}/resolve",
                       data={"status": "fixed", "comment": "x"}).status_code == 403
    # Côté propriétaire.
    tenancy.add_membership(db_session, user_id=me.id, org_id=prod.id, role="editor")
    received = client.get(f"/o/{prod.slug}/reports")
    assert received.status_code == 200 and "Loi modifiée en 2026." in received.text
    assert "1 signalement(s) ouvert(s)" in client.get(f"/o/{prod.slug}/tests/{t.test_id}").text
    assert "/resolve" in client.get(f"/o/{prod.slug}/reports/{report.id}").text
    r = client.post(f"/o/{prod.slug}/reports/{report.id}/resolve", data={"status": "rejected", "comment": "Toujours en vigueur."})
    assert r.status_code == 303
    db_session.refresh(report)
    assert report.status == "rejected" and report.resolved_by == me.id
    assert client.post(f"/o/{prod.slug}/reports/{report.id}/resolve",
                       data={"status": "fixed", "comment": "x"}).status_code == 409
    # Réglage UI du détecteur de citations.
    tenancy.set_membership_role(db_session, user_id=me.id, org_id=prod.id, role="org_admin")
    assert client.post(f"/o/{prod.slug}/settings/detectors", data={"citation_drop_points": "15"}).status_code == 303
    page = client.get(f"/o/{prod.slug}/settings/detectors")
    assert page.status_code == 200 and "chute de 15 points" in page.text
    assert client.post(f"/o/{prod.slug}/settings/detectors", data={"citation_drop_points": "0"}).status_code == 400


def test_api_signalements_et_reglage_citations(client, db_session, monde):
    prod, cons, t = monde["prod"], monde["cons"], monde["t"]
    pool = pools.create(db_session, prod, name="Pool API E7s", visibility="all")
    pools.add_tests(db_session, pool, [t.test_id])
    pools.subscribe(db_session, _perimeter(db_session, cons, "api"), pool.id)
    hv_c, hv_p, he_p = _bearer(db_session, cons, "viewer"), _bearer(db_session, prod, "viewer"), _bearer(db_session, prod, "editor")
    h_tiers = _bearer(db_session, monde["tiers"], "viewer")
    base_c, base_p = f"/api/v1/orgs/{cons.slug}/reports", f"/api/v1/orgs/{prod.slug}/reports"
    assert client.post(f"/api/v1/orgs/{monde['tiers'].slug}/reports", headers=h_tiers,
                       json={"test_id": t.test_id, "category": "obsolete"}).status_code == 404
    assert client.post(base_c, headers=hv_c, json={"test_id": t.test_id, "category": "spam"}).status_code == 422
    assert client.post(base_c, headers=hv_c, json={"test_id": t.test_id, "category": "other"}).status_code == 400
    r = client.post(base_c, headers=hv_c, json={"test_id": t.test_id, "category": "ambiguous", "comment": "Quel délai ?"})
    assert r.status_code == 201
    body = r.json()
    assert (body["owner_org_slug"], body["reporter_org_slug"], body["owned"], body["status"]) == (prod.slug, cons.slug, False, "open")
    rid = body["id"]
    assert [x["id"] for x in client.get(base_c, headers=hv_c, params={"tab": "sent"}).json()] == [rid]
    assert client.get(base_c, headers=hv_c).json() == [], "rien de reçu côté consommateur"
    received = client.get(base_p, headers=hv_p, params={"status": "open"}).json()
    assert [x["id"] for x in received] == [rid] and received[0]["owned"] is True and received[0]["test_prompt"] == t.prompt
    assert client.get(f"/api/v1/orgs/{monde['tiers'].slug}/reports/{rid}", headers=h_tiers).status_code == 404
    assert client.post(f"{base_p}/{rid}/resolve", headers=hv_p, json={"status": "fixed", "comment": "ok"}).status_code == 403
    assert client.post(f"{base_c}/{rid}/resolve", headers=_bearer(db_session, cons, "editor"),
                       json={"status": "fixed", "comment": "ok"}).status_code == 403, "pas propriétaire"
    assert client.post(f"{base_p}/{rid}/resolve", headers=he_p, json={"status": "open", "comment": "ok"}).status_code == 422
    r = client.post(f"{base_p}/{rid}/resolve", headers=he_p, json={"status": "fixed", "comment": "Précisé."})
    assert r.status_code == 200 and r.json()["status"] == "fixed" and r.json()["resolution_comment"] == "Précisé."
    assert client.get(f"{base_c}/{rid}", headers=hv_c).json()["status"] == "fixed"
    assert client.post(f"{base_p}/{rid}/resolve", headers=he_p, json={"status": "fixed", "comment": "x"}).status_code == 409
    # Réglage du détecteur de citations via l'API.
    h_admin = _bearer(db_session, prod, "org_admin")
    ds = f"/api/v1/orgs/{prod.slug}/detector-settings"
    assert client.get(ds, headers=he_p).json()["effective_citation_drop_points"] == "20"
    r = client.put(ds, json={"citation_drop_points": "12.5"}, headers=h_admin)
    assert r.status_code == 200 and r.json()["effective_citation_drop_points"] == "12.50"
    assert r.json()["citation_drop_from_org_slug"] == prod.slug and r.json()["own"]["always_wrong_runs"] is None
    assert client.put(ds, json={"citation_drop_points": 0}, headers=h_admin).status_code == 422
    assert db_session.execute(select(Notification).where(Notification.kind == "report_opened",
                                                         Notification.organization_id == prod.id)).first() is not None
