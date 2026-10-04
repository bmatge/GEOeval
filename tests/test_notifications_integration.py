"""Notifications et détecteurs (E7) sur PostgreSQL."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from geoeval.db.models import Model, Notification, Organization, Perimeter, RunEvaluation, RunRow, User
from geoeval.web import api_tokens, budget, budget_alerts, contracts, detectors, mailer, notifications, services, tenancy
from tests.conftest import TEST_USER_EMAIL

pytestmark = pytest.mark.integration

PREFIX = "e7-"
DOMAIN = "@e7.test"


@pytest.fixture()
def clean(db_session):
    def _purge():
        ids = [o.id for o in db_session.execute(select(Organization).where(Organization.slug.like(PREFIX + "%"))).scalars()]
        p = {"ids": ids or [-1], "d": "%" + DOMAIN}
        tests_of = "SELECT test_id FROM tests WHERE organization_id = ANY(:ids)"
        runs_of = "SELECT run_id FROM runs WHERE organization_id = ANY(:ids)"
        db_session.execute(text(f"DELETE FROM run_evaluations WHERE run_id IN ({runs_of}) OR test_id IN ({tests_of})"), p)
        db_session.execute(text(f"DELETE FROM run_results WHERE run_id IN ({runs_of})"), p)
        db_session.execute(text("DELETE FROM usage WHERE organization_id = ANY(:ids)"), p)
        db_session.execute(text("DELETE FROM runs WHERE organization_id = ANY(:ids)"), p)
        db_session.execute(text("DELETE FROM notifications WHERE organization_id = ANY(:ids) "
                                "OR user_id IN (SELECT id FROM users WHERE email LIKE :d)"), p)
        db_session.execute(text("DELETE FROM job_logs WHERE job_id IN (SELECT id FROM jobs WHERE organization_id = ANY(:ids))"), p)
        db_session.execute(text(f"DELETE FROM test_ground_truth WHERE test_id IN ({tests_of})"), p)
        for tbl, col in (("jobs", "organization_id"), ("budget_alerts", "organization_id"), ("budgets", "organization_id"),
                         ("llm_contracts", "organization_id"), ("detector_settings", "organization_id"),
                         ("tests", "organization_id"), ("perimeters", "organization_id"), ("api_tokens", "organization_id"),
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


@pytest.fixture()
def monde(db_session, clean):
    m = tenancy.create_org(db_session, name="Ministère E7", slug=PREFIX + "min")
    d = tenancy.create_org(db_session, name="Direction E7", slug=PREFIX + "dir", parent=m)
    s = tenancy.create_org(db_session, name="Service E7", slug=PREFIX + "svc", parent=d)
    return dict(m=m, d=d, s=s, admin_m=_user(db_session, "admin-m", m, "org_admin"),
                editor_s=_user(db_session, "editor-s", s, "editor"), viewer_s=_user(db_session, "viewer-s", s, "viewer"),
                admin_s=_user(db_session, "admin-s", s, "org_admin"))


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


def _inbox(db, user):
    return notifications.list_for_user(db, user.id)


# ---------------------------------------------------------------------
# Émission, destinataires, préférences
# ---------------------------------------------------------------------
def test_destinataires_par_role_et_idempotence(db_session, monde, smtp):
    s, editor, viewer, admin_s = monde["s"], monde["editor_s"], monde["viewer_s"], monde["admin_s"]
    e = notifications.notify(db_session, s, "job_failed", title="Échec", body="détail", link="/o/x/jobs/1",
                             dedup_key="job_failed:test-1")
    assert {n.user_id for n in e.created} == {editor.id, admin_s.id}, "editor+ : le viewer n'est pas notifié"
    assert e.email_status == "sent" and set(smtp[-1]["To"].split(", ")) == {editor.email, admin_s.email}
    assert _inbox(db_session, viewer) == []
    again = notifications.notify(db_session, s, "job_failed", title="Échec", dedup_key="job_failed:test-1")
    assert again.created == [] and len(smtp) == 1, "même clé : ni doublon ni second email"


def test_repli_sur_l_ancetre_et_sans_destinataire(db_session, monde):
    d = monde["d"]
    e = notifications.notify(db_session, d, "contract_expired", title="Contrat expiré")
    assert [n.user_id for n in e.created] == [monde["admin_m"].id] and e.recipient_org.id == monde["m"].id
    orphan = tenancy.create_org(db_session, name="Orpheline E7", slug=PREFIX + "orpheline")
    assert notifications.notify(db_session, orphan, "job_failed", title="x").email_status == "no_recipient"


def test_preferences_email(db_session, monde, smtp):
    s, editor, admin_s = monde["s"], monde["editor_s"], monde["admin_s"]
    assert notifications.preferences(db_session, editor.id)["always_wrong"] == "none"
    e = notifications.notify(db_session, s, "always_wrong", title="Toujours faux", dedup_key="aw:1")
    assert len(e.created) == 2 and e.emailed == [] and e.email_status == "disabled" and smtp == []
    notifications.set_preferences(db_session, editor.id, {"always_wrong": True})
    e = notifications.notify(db_session, s, "always_wrong", title="Toujours faux", dedup_key="aw:2")
    assert e.emailed == [editor.email] and smtp[-1]["To"] == editor.email
    statuses = {n.user_id: n.email_status for n in e.created}
    assert statuses == {editor.id: "sent", admin_s.id: "none"}
    with pytest.raises(ValueError):
        notifications.set_preferences(db_session, editor.id, {"inconnu": True})


def test_lecture_et_cloisonnement(db_session, monde):
    s, editor, admin_s = monde["s"], monde["editor_s"], monde["admin_s"]
    notifications.notify(db_session, s, "job_failed", title="A", dedup_key="a")
    notifications.notify(db_session, s, "job_failed", title="B", dedup_key="b")
    assert notifications.unread_count(db_session, editor.id) == 2
    mine = _inbox(db_session, editor)
    assert [n.title for n in mine] == ["B", "A"], "plus récente d'abord"
    theirs = _inbox(db_session, admin_s)[0]
    assert notifications.mark_read(db_session, editor.id, theirs.id) == 0, "jamais la notification d'un autre"
    assert notifications.mark_read(db_session, editor.id, mine[0].id) == 1
    assert notifications.unread_count(db_session, editor.id) == 1
    assert notifications.mark_read(db_session, editor.id) == 1 and notifications.unread_count(db_session, editor.id) == 0
    assert notifications.unread_count(db_session, admin_s.id) == 2


def test_alerte_budget_passe_par_les_notifications(db_session, monde, smtp):
    m, s, admin_m = monde["m"], monde["s"], monde["admin_m"]
    model_id = db_session.execute(select(Model.model_id).order_by(Model.model_id)).scalars().first()
    budget.set_cap(db_session, org_id=m.id, cap_eur=Decimal("10"), updated_by=None)
    from geoeval.db.models import UsageRecord
    db_session.add(UsageRecord(organization_id=s.id, model_id=model_id, run_id=None, kind="tested", billed_to="platform",
                               input_tokens=0, output_tokens=0, cost_eur=Decimal("9")))
    db_session.commit()
    [alert] = budget_alerts.evaluate(db_session, s.id)
    assert alert.email_status == "sent" and alert.emailed_to == [admin_m.email]
    [n] = _inbox(db_session, admin_m)
    assert n.kind == "budget_threshold" and n.link == f"/o/{m.slug}/budget" and n.payload["threshold"] == 80
    # Email coupé : l'alerte reste visible in-app, statut « disabled ».
    notifications.set_preferences(db_session, admin_m.id, {"budget_threshold": False})
    db_session.add(UsageRecord(organization_id=s.id, model_id=model_id, run_id=None, kind="tested", billed_to="platform",
                               input_tokens=0, output_tokens=0, cost_eur=Decimal("2")))
    db_session.commit()
    [alert100] = budget_alerts.evaluate(db_session, s.id)
    assert alert100.threshold == 100 and alert100.email_status == "disabled" and len(smtp) == 1
    assert len(_inbox(db_session, admin_m)) == 2


# ---------------------------------------------------------------------
# Détecteurs
# ---------------------------------------------------------------------
def test_job_en_echec(db_session, monde, smtp):
    s, editor = monde["s"], monde["editor_s"]
    assert detectors.job_failed(s.id, "abcdef123456", "HTTP 401 (clé invalide)") == 2
    [n] = _inbox(db_session, editor)
    assert n.kind == "job_failed" and "HTTP 401" in n.body and n.link == f"/o/{s.slug}/jobs/abcdef123456"
    assert detectors.job_failed(s.id, "abcdef123456", "encore") == 0, "un job, une notification"


def test_job_en_echec_depuis_le_worker(db_session, monde, monkeypatch):
    from geoeval.worker import jobs

    def boom(session, **kw):
        raise RuntimeError("notateur indisponible")

    monkeypatch.setattr(jobs, "select_tests", lambda session, **kw: [object()])
    monkeypatch.setattr(jobs, "execute_run", boom)
    monkeypatch.setattr(jobs, "HEARTBEAT_SECONDS", 3600)
    job = jobs.submit(db_session, dict(organization_id=monde["s"].id, tested_models=["m"], judges=[]))
    db_session.execute(text("UPDATE jobs SET status='running' WHERE id=:i"), {"i": job.id})
    db_session.commit()
    jobs.execute(job.id)
    [n] = _inbox(db_session, monde["editor_s"])
    assert n.kind == "job_failed" and "notateur indisponible" in n.body and n.payload["job_id"] == job.id


def test_contrats_expirants_puis_expires(db_session, monde, monkeypatch):
    from cryptography.fernet import Fernet
    from geoeval.web import crypto
    monkeypatch.setenv("GEOEVAL_KEY_SECRET", Fernet.generate_key().decode())
    crypto._fernet.cache_clear()
    d, admin_m = monde["d"], monde["admin_m"]
    today = date(2026, 10, 1)
    c = contracts.create(db_session, d, family="mistral", label="Marché E7", valid_to=today + timedelta(days=20))
    assert detectors.check_contracts(db_session, today=today) == 1
    assert detectors.check_contracts(db_session, today=today + timedelta(days=1)) == 0, "même palier : pas de doublon"
    assert detectors.check_contracts(db_session, today=today + timedelta(days=14)) == 1, "palier J-7"
    assert detectors.check_contracts(db_session, today=today + timedelta(days=21)) == 1
    kinds = [n.kind for n in _inbox(db_session, admin_m)]
    assert kinds == ["contract_expired", "contract_expiring", "contract_expiring"]
    assert detectors.check_contracts(db_session, today=today + timedelta(days=40)) == 0
    contracts.update(db_session, c, is_active=False)
    contracts.update(db_session, c, valid_to=today + timedelta(days=200))
    contracts.update(db_session, c, is_active=True)
    assert detectors.check_contracts(db_session, today=today) == 0, "hors horizon de 30 jours"
    crypto._fernet.cache_clear()


def _corpus(db, org):
    peri = Perimeter(organization_id=org.id, name="Site E7", slug="site-e7", kind="site")
    db.add(peri)
    db.commit()
    t = services.create_test(db, org.id, perimeter_id=peri.id, prompt="Quelle est la capitale ?", expected_answer="Paris",
                             response_quality_prompt_id=None, citation_quality_prompt_id=None)
    tested = db.execute(select(Model).where(Model.model_name == "openrouter").order_by(Model.model_id)).scalars().first()
    judges = db.execute(select(Model).where(Model.model_name == "albert").order_by(Model.model_id)).scalars().all()[:2]
    return t, tested, judges


def _run(db, org, test, tested, judges, scores):
    r = RunRow(organization_id=org.id, tested_model_id=tested.model_id)
    db.add(r)
    db.commit()
    for j, sc in zip(judges, scores):
        db.add(RunEvaluation(run_id=r.run_id, test_id=test.test_id, judge_model_id=j.model_id, judge_run_index=1,
                             response_quality_score=sc, citation_quality_score=sc))
    db.commit()
    return r.run_id


def test_question_toujours_fausse_une_alerte_par_serie(db_session, monde):
    s, editor = monde["s"], monde["editor_s"]
    t, tested, judges = _corpus(db_session, s)
    aw = lambda: [n for n in _inbox(db_session, editor) if n.kind == "always_wrong"]  # noqa: E731
    r1 = _run(db_session, s, t, tested, judges, [3, 4])
    r2 = _run(db_session, s, t, tested, judges, [2, 6])          # moyenne 4 < 5
    assert detectors.check_always_wrong(db_session, r1) == 0 and detectors.check_always_wrong(db_session, r2) == 0
    r3 = _run(db_session, s, t, tested, judges, [1, 1])
    assert detectors.check_always_wrong(db_session, r3) == 2     # editor + admin du service
    [n] = aw()
    assert n.payload["run_ids"] == [r3, r2, r1] and n.link == f"/o/{s.slug}/runs/{r3}" and tested.model_version in n.title
    r4 = _run(db_session, s, t, tested, judges, [0, 2])
    assert detectors.check_always_wrong(db_session, r4) == 0 and len(aw()) == 1, "la série continue : pas de nouvelle alerte"
    _run(db_session, s, t, tested, judges, [8, 9])                # la série casse
    for _ in range(2):
        detectors.check_always_wrong(db_session, _run(db_session, s, t, tested, judges, [1, 2]))
    assert len(aw()) == 1
    detectors.check_always_wrong(db_session, _run(db_session, s, t, tested, judges, [1, 2]))
    assert len(aw()) == 2, "nouvelle série de 3 : nouvelle alerte"
    assert detectors.after_evaluation_safely(999_999_999) == 0


def test_reglages_herites_du_detecteur(db_session, monde):
    m, d, s = monde["m"], monde["d"], monde["s"]
    assert (detectors.effective_settings(db_session, s).runs, detectors.effective_settings(db_session, s).runs_from) == (3, None)
    detectors.set_settings(db_session, m, runs=2, threshold=None)
    detectors.set_settings(db_session, d, runs=None, threshold="7")
    eff = detectors.effective_settings(db_session, s)
    assert (eff.runs, eff.runs_from.id, eff.threshold, eff.threshold_from.id) == (2, m.id, Decimal("7"), d.id)
    for bad in (dict(runs=1, threshold=None), dict(runs=None, threshold="11"), dict(runs=None, threshold="x")):
        with pytest.raises(ValueError):
            detectors.set_settings(db_session, s, **bad)
    assert detectors.set_settings(db_session, d, runs=None, threshold=None) is None
    # Effet : avec 2 évaluations sous 5 (réglage hérité du ministère), l'alerte part.
    t, tested, judges = _corpus(db_session, s)
    _run(db_session, s, t, tested, judges, [4, 4])
    assert detectors.check_always_wrong(db_session, _run(db_session, s, t, tested, judges, [3, 3])) == 2


# ---------------------------------------------------------------------
# UI et API
# ---------------------------------------------------------------------
def _ci_user(db):
    return db.execute(select(User).where(User.email == TEST_USER_EMAIL)).scalar_one()


def test_ui_boite_de_reception_preferences_reglages(client, db_session, monde):
    client.get("/")  # provisionne l'utilisateur de test
    me = _ci_user(db_session)
    s = monde["s"]
    tenancy.add_membership(db_session, user_id=me.id, org_id=s.id, role="org_admin")
    notifications.notify(db_session, s, "job_failed", title="Échec UI E7", body="détail", link=f"/o/{s.slug}/jobs/x",
                         dedup_key="ui-1")
    db_session.add(Notification(user_id=me.id, organization_id=s.id, kind="job_failed", title="Lien externe E7",
                                link="https://exemple.org/piege"))
    db_session.commit()
    page = client.get("/notifications")
    assert page.status_code == 200 and "Échec UI E7" in page.text and "non lue" in page.text
    assert 'aria-label="2 non lue(s)"' in page.text, "compteur de l'en-tête"
    mine = {n.title: n for n in notifications.list_for_user(db_session, me.id)}
    r = client.get(f"/notifications/{mine['Échec UI E7'].id}/open")
    assert r.status_code == 303 and r.headers["location"] == f"/o/{s.slug}/jobs/x"
    r = client.get(f"/notifications/{mine['Lien externe E7'].id}/open")
    assert r.headers["location"] == "/notifications", "pas de redirection ouverte"
    other = notifications.list_for_user(db_session, monde["editor_s"].id)[0]
    assert client.get(f"/notifications/{other.id}/open").status_code == 404
    assert notifications.unread_count(db_session, me.id) == 0
    r = client.post("/notifications/preferences", data={"mode_always_wrong": "immediate", "mode_job_failed": "none",
                                                        "mode_citation_drop": "digest"})
    assert r.status_code == 303
    prefs = notifications.preferences(db_session, me.id)
    assert (prefs["always_wrong"], prefs["job_failed"], prefs["citation_drop"]) == ("immediate", "none", "digest")
    assert client.post("/notifications/preferences", data={"mode_job_failed": "fax"}).status_code == 400
    assert "Préférences de notification" in client.get("/notifications/preferences").text
    assert client.post(f"/o/{s.slug}/settings/detectors", data={"always_wrong_runs": "4", "always_wrong_threshold": "6"}).status_code == 303
    page = client.get(f"/o/{s.slug}/settings/detectors")
    assert page.status_code == 200 and "4 évaluations d'affilée sous 6" in page.text
    assert client.post(f"/o/{s.slug}/settings/detectors", data={"always_wrong_runs": "1"}).status_code == 400
    assert client.post("/notifications/read-all").status_code == 303


def _bearer(db, org, role):
    _, plain = api_tokens.create(db, org_id=org.id, name=f"tok-{role}", role=role, created_by=None)
    return {"Authorization": f"Bearer {plain}"}


def test_api_notifications_et_reglages(client, db_session, monde):
    client.get("/")
    me = _ci_user(db_session)
    s = monde["s"]
    db_session.add_all([Notification(user_id=me.id, organization_id=s.id, kind="always_wrong", title="API E7 a"),
                        Notification(user_id=me.id, organization_id=s.id, kind="job_failed", title="API E7 b")])
    db_session.commit()
    body = client.get("/api/v1/me/notifications", params={"unread_only": True}).json()
    titles = {i["title"] for i in body["items"]}
    assert {"API E7 a", "API E7 b"} <= titles and body["unread"] >= 2
    nid = next(i["id"] for i in body["items"] if i["title"] == "API E7 a")
    assert client.post(f"/api/v1/me/notifications/{nid}/read").status_code == 200
    other = db_session.execute(select(Notification.id).where(Notification.user_id != me.id)).scalars().first()
    if other:
        assert client.post(f"/api/v1/me/notifications/{other}/read").status_code == 404
    assert client.post("/api/v1/me/notifications/read-all").json()["unread"] == 0
    h_admin, h_editor = _bearer(db_session, s, "org_admin"), _bearer(db_session, s, "editor")
    assert client.get("/api/v1/me/notifications", headers=h_admin).status_code == 403, "jeton : pas de boîte personnelle"
    r = client.put("/api/v1/me/notification-preferences", json={"modes": {"always_wrong": "digest"}})
    assert r.status_code == 200 and r.json()["modes"]["always_wrong"] == "digest"
    assert client.put("/api/v1/me/notification-preferences", json={"modes": {"x": "none"}}).status_code == 400
    assert client.put("/api/v1/me/notification-preferences", json={"modes": {"always_wrong": "fax"}}).status_code == 422
    base = f"/api/v1/orgs/{s.slug}/detector-settings"
    assert client.get(base, headers=h_editor).json()["effective_runs"] == 3
    assert client.put(base, json={"always_wrong_runs": 5}, headers=h_editor).status_code == 403
    r = client.put(base, json={"always_wrong_runs": 5, "always_wrong_threshold": "6.5"}, headers=h_admin)
    assert r.status_code == 200 and r.json()["effective_runs"] == 5 and r.json()["runs_from_org_slug"] == s.slug
    assert client.put(base, json={"always_wrong_runs": 99}, headers=h_admin).status_code == 422
