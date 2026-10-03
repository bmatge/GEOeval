"""Budget consolidé, alertes de seuil et sauts du planificateur (E3)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from geoeval.db.models import BudgetAlert, Job, Model, Organization, Perimeter, ScheduledRun, UsageRecord
from geoeval.web import api_tokens, budget, budget_alerts, mailer, tenancy

pytestmark = pytest.mark.integration

PREFIX = "e3-"


@pytest.fixture()
def clean(db_session):
    def _purge():
        ids = [o.id for o in db_session.execute(select(Organization).where(Organization.slug.like(PREFIX + "%"))).scalars()]
        if ids:
            db_session.execute(text("DELETE FROM job_logs WHERE job_id IN (SELECT id FROM jobs WHERE organization_id = ANY(:ids))"), {"ids": ids})
            for tbl, col in (("jobs", "organization_id"), ("scheduled_runs", "organization_id"), ("perimeters", "organization_id"),
                             ("usage", "organization_id"), ("budget_alerts", "organization_id"), ("budgets", "organization_id"),
                             ("api_tokens", "organization_id"), ("memberships", "org_id"), ("audit_log", "org_id")):
                db_session.execute(text(f"DELETE FROM {tbl} WHERE {col} = ANY(:ids)"), {"ids": ids})
            db_session.execute(text("UPDATE organizations SET parent_id = NULL WHERE id = ANY(:ids)"), {"ids": ids})
            db_session.execute(text("DELETE FROM organizations WHERE id = ANY(:ids)"), {"ids": ids})
        db_session.commit()
    _purge()
    yield
    _purge()


def _mk(db, slug, parent=None, name=None, kind=None):
    return tenancy.create_org(db, name=name or slug, slug=PREFIX + slug, parent=parent, kind=kind)


@pytest.fixture()
def arbre(db_session, clean):
    m = _mk(db_session, "min", name="Ministère E3", kind="ministere")
    d = _mk(db_session, "dir", parent=m, name="Direction E3", kind="direction")
    s = _mk(db_session, "svc", parent=d, name="Service E3", kind="service")
    other = _mk(db_session, "autre", name="Autre E3")
    return dict(m=m, d=d, s=s, other=other)


@pytest.fixture()
def spend(db_session):
    model_id = db_session.execute(select(Model.model_id).order_by(Model.model_id)).scalars().first()

    def _spend(org, eur):
        db_session.add(UsageRecord(organization_id=org.id, model_id=model_id, run_id=None, kind="tested",
                                   billed_to="platform", input_tokens=0, output_tokens=0, cost_eur=Decimal(str(eur))))
        db_session.commit()
    return _spend


# ---------------------------------------------------------------------
# Consolidation et contrôle
# ---------------------------------------------------------------------
def test_depense_consolidee_vers_les_ancetres(db_session, arbre, spend):
    spend(arbre["s"], 3)
    spend(arbre["d"], 2)
    spend(arbre["other"], 50)
    assert budget.subtree_period_spent(db_session, arbre["m"], "month") == Decimal("5")
    assert budget.subtree_period_spent(db_session, arbre["d"], "month") == Decimal("5")
    assert budget.subtree_period_spent(db_session, arbre["s"], "day") == Decimal("3")
    assert budget.own_period_spent(db_session, arbre["d"].id, "month") == Decimal("2")
    assert budget.current_period_spent(db_session, arbre["m"].id, "month") == Decimal("5")


def test_plafond_du_ministere_bloque_un_service(db_session, arbre, spend):
    budget.set_cap(db_session, org_id=arbre["m"].id, cap_eur=Decimal("10"), updated_by=None)
    spend(arbre["d"], 9)
    ok = budget.check_budget(db_session, org_id=arbre["s"].id, estimate_eur=Decimal("0.5"))
    assert ok.ok and ok.reason == "ok"
    ko = budget.check_budget(db_session, org_id=arbre["s"].id, estimate_eur=Decimal("2"))
    assert not ko.ok and ko.blocking_org_id == arbre["m"].id
    assert "Budget mensuel dépassé (plafond de « Ministère E3 »)" in ko.reason
    assert budget.check_budget(db_session, org_id=arbre["other"].id, estimate_eur=Decimal("100")).ok


def test_plafond_journalier_d_une_direction(db_session, arbre, spend):
    budget.set_cap(db_session, org_id=arbre["d"].id, cap_eur=Decimal("1000"), daily_cap_eur=Decimal("1"), updated_by=None)
    spend(arbre["s"], 1)
    ko = budget.check_budget(db_session, org_id=arbre["s"].id, estimate_eur=Decimal("0.01"))
    assert not ko.ok and "Budget journalier dépassé (plafond de « Direction E3 »)" in ko.reason


def test_plafond_propre_et_herite_le_plus_contraignant(db_session, arbre, spend):
    budget.set_cap(db_session, org_id=arbre["m"].id, cap_eur=Decimal("100"), updated_by=None)
    budget.set_cap(db_session, org_id=arbre["s"].id, cap_eur=Decimal("5"), updated_by=None)
    spend(arbre["s"], 4)
    ko = budget.check_budget(db_session, org_id=arbre["s"].id, estimate_eur=Decimal("2"))
    assert not ko.ok and ko.blocking_org_id == arbre["s"].id and "Budget mensuel dépassé :" in ko.reason
    cs = budget.chain_constraints(db_session, arbre["s"])
    assert [(c.owner.id, c.inherited) for c in cs] == [(arbre["s"].id, False), (arbre["m"].id, True)]


# ---------------------------------------------------------------------
# Alertes
# ---------------------------------------------------------------------
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

        def starttls(self):
            pass

        def login(self, *a):
            pass

        def send_message(self, msg):
            sent.append(msg)

    monkeypatch.setattr(mailer.smtplib, "SMTP", Fake)
    monkeypatch.setenv("GEOEVAL_SMTP_HOST", "smtp.test")
    return sent


def _admin(db, org, email):
    from geoeval.web.auth import load_or_provision_user

    cu = load_or_provision_user(db, email, groups=[])
    tenancy.add_membership(db, user_id=cu.id, org_id=org.id, role="org_admin")


def test_alertes_80_puis_100_une_seule_fois(db_session, arbre, spend, smtp):
    _admin(db_session, arbre["m"], "admin-min@e3.test")
    budget.set_cap(db_session, org_id=arbre["m"].id, cap_eur=Decimal("10"), updated_by=None)
    spend(arbre["s"], 8.5)
    created = budget_alerts.evaluate(db_session, arbre["s"].id)
    assert [(a.organization_id, a.threshold, a.email_status) for a in created] == [(arbre["m"].id, 80, "sent")]
    assert smtp[-1]["To"] == "admin-min@e3.test" and "80 %" in smtp[-1]["Subject"]
    assert budget_alerts.evaluate(db_session, arbre["s"].id) == [], "jamais deux fois la même alerte"
    spend(arbre["d"], 2)
    created = budget_alerts.evaluate(db_session, arbre["d"].id)
    assert [a.threshold for a in created] == [100] and len(smtp) == 2
    rows = db_session.execute(select(BudgetAlert).where(BudgetAlert.organization_id == arbre["m"].id)).scalars().all()
    assert sorted(r.threshold for r in rows) == [80, 100]
    assert {r.period_key for r in rows} == {budget_alerts.period_key(db_session, "month")}


def test_destinataires_de_l_ancetre_le_plus_proche(db_session, arbre, spend, smtp):
    _admin(db_session, arbre["m"], "admin-min2@e3.test")
    budget.set_cap(db_session, org_id=arbre["d"].id, cap_eur=Decimal("1"), updated_by=None)
    spend(arbre["s"], 1)
    created = budget_alerts.evaluate(db_session, arbre["s"].id)
    assert created and created[0].organization_id == arbre["d"].id
    assert created[0].emailed_to == ["admin-min2@e3.test"], "la direction n'a pas d'admin : on remonte au ministère"


def test_alerte_sans_smtp_reste_tracee(db_session, arbre, spend, monkeypatch):
    monkeypatch.delenv("GEOEVAL_SMTP_HOST", raising=False)
    budget.set_cap(db_session, org_id=arbre["m"].id, cap_eur=Decimal("1"), updated_by=None)
    spend(arbre["m"], 1)
    created = budget_alerts.evaluate(db_session, arbre["m"].id)
    assert created and {a.email_status for a in created} == {"no_recipient"}, "aucun org_admin sur la chaîne"


def test_fin_de_job_declenche_l_evaluation(db_session, arbre, spend, monkeypatch):
    from geoeval.worker import jobs

    budget.set_cap(db_session, org_id=arbre["m"].id, cap_eur=Decimal("10"), updated_by=None)

    def fake_run(session, *, tested_model, tests, **kw):
        spend(arbre["s"], 9)
        return 1

    monkeypatch.setattr(jobs, "select_tests", lambda session, **kw: [object()])
    monkeypatch.setattr(jobs, "execute_run", fake_run)
    monkeypatch.setattr(jobs, "evaluate_run", lambda session, **kw: None)
    monkeypatch.setattr(jobs, "HEARTBEAT_SECONDS", 3600)
    job = jobs.submit(db_session, dict(organization_id=arbre["s"].id, tested_models=["m"], judges=[]))
    db_session.execute(text("UPDATE jobs SET status='running' WHERE id=:i"), {"i": job.id})
    db_session.commit()
    jobs.execute(job.id)
    alerts = db_session.execute(select(BudgetAlert).where(BudgetAlert.organization_id == arbre["m"].id)).scalars().all()
    assert [a.threshold for a in alerts] == [80]


# ---------------------------------------------------------------------
# Planificateur : saut tracé
# ---------------------------------------------------------------------
def _schedule(db, org, kind="daily", config=None):
    slug = f"site-e3-{org.id}"
    peri = db.execute(select(Perimeter).where(Perimeter.organization_id == org.id, Perimeter.slug == slug)).scalar_one_or_none()
    if peri is None:
        peri = Perimeter(organization_id=org.id, name="Site E3", slug=slug, kind="site")
        db.add(peri)
        db.commit()
    sr = ScheduledRun(organization_id=org.id, perimeter_id=peri.id, name=f"Prog {kind}", tested_models=["m"],
                      judges=[{"model": "j", "repeats": 1}], test_ids=None, schedule_kind=kind,
                      schedule_config=config or {"time": "09:00"}, enabled=True,
                      next_run_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    db.add(sr)
    db.commit()
    return sr


def test_echeance_sautee_si_plafond_depasse(db_session, arbre, spend):
    from geoeval.worker import scheduler

    budget.set_cap(db_session, org_id=arbre["m"].id, cap_eur=Decimal("1"), updated_by=None)
    spend(arbre["s"], 2)
    daily = _schedule(db_session, arbre["s"])
    once = _schedule(db_session, arbre["s"], "once", {"at": "2026-01-01T08:00"})
    before = datetime.now(timezone.utc)
    assert scheduler.tick_if_leader(db_session) == 0
    db_session.expire_all()
    daily, once = db_session.get(ScheduledRun, daily.schedule_id), db_session.get(ScheduledRun, once.schedule_id)
    assert "plafond de « Ministère E3 »" in daily.last_skip_reason and daily.last_skipped_at >= before
    assert daily.enabled and daily.next_run_at > before and daily.last_job_id is None
    assert once.enabled is False and once.next_run_at is None and once.last_skip_reason
    assert db_session.execute(select(Job).where(Job.organization_id == arbre["s"].id)).scalars().first() is None
    audit = db_session.execute(text("SELECT count(*) FROM audit_log WHERE action='skip_budget' AND org_id=:o"), {"o": arbre["s"].id}).scalar_one()
    assert audit == 2
    assert db_session.execute(select(BudgetAlert).where(BudgetAlert.organization_id == arbre["m"].id, BudgetAlert.threshold == 100)).scalars().first()


def test_echeance_executee_efface_le_motif(db_session, arbre, spend):
    from geoeval.worker import scheduler

    sr = _schedule(db_session, arbre["s"])
    sr.last_skip_reason = "ancien motif"
    db_session.commit()
    assert scheduler.tick_if_leader(db_session) == 1
    db_session.expire_all()
    sr = db_session.get(ScheduledRun, sr.schedule_id)
    assert sr.last_skip_reason is None and sr.last_job_id


# ---------------------------------------------------------------------
# UI, API, métriques
# ---------------------------------------------------------------------
def test_page_budget_et_bandeaux(client, db_session, arbre, spend, monkeypatch):
    budget.set_cap(db_session, org_id=arbre["m"].id, cap_eur=Decimal("10"), updated_by=None)
    spend(arbre["s"], 9)
    r = client.get(f"/o/{arbre['s'].slug}/budget")
    assert r.status_code == 200
    assert "Ministère E3" in r.text and "hérité" in r.text and "Budget mensuel de « Ministère E3 »" in r.text
    assert "Budget mensuel de « Ministère E3 »" in client.get(f"/o/{arbre['s'].slug}/launch").text
    assert "Budget mensuel de « Ministère E3 »" in client.get(f"/o/{arbre['s'].slug}/dashboard").text
    monkeypatch.delenv("DEV_FAKE_EMAIL", raising=False)
    assert "Budget mensuel" not in client.get(f"/o/{arbre['s'].slug}/dashboard").text, "rien pour un anonyme"


def test_planification_sautee_visible(client, db_session, arbre, spend):
    from geoeval.worker import scheduler

    budget.set_cap(db_session, org_id=arbre["m"].id, cap_eur=Decimal("1"), updated_by=None)
    spend(arbre["s"], 2)
    sr = _schedule(db_session, arbre["s"])
    scheduler.tick_if_leader(db_session)
    assert "plafond de « Ministère E3 »" in client.get(f"/o/{arbre['s'].slug}/schedules").text
    body = client.get(f"/api/v1/orgs/{arbre['s'].slug}/schedules/{sr.schedule_id}").json()
    assert body["last_skip_reason"] and body["last_skipped_at"]


def test_api_budget(anonymous_client, db_session, arbre, spend):
    budget.set_cap(db_session, org_id=arbre["m"].id, cap_eur=Decimal("10"), updated_by=None)
    budget.set_cap(db_session, org_id=arbre["s"].id, cap_eur=Decimal("50"), daily_cap_eur=Decimal("5"), updated_by=None)
    spend(arbre["s"], 9)
    _, plain = api_tokens.create(db_session, org_id=arbre["m"].id, name="ed", role="editor", created_by=None)
    r = anonymous_client.get(f"/api/v1/orgs/{arbre['s'].slug}/budget", headers={"Authorization": f"Bearer {plain}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["monthly_cap_eur"] == 50 and body["daily_cap_eur"] == 5 and body["month_spent_eur"] == 9
    levels = {(c["owner_slug"], c["period"]): (c["inherited"], c["level"]) for c in body["constraints"]}
    assert levels[(arbre["m"].slug, "month")] == (True, "warning")
    assert levels[(arbre["s"].slug, "day")] == (False, "exceeded")
    _, viewer = api_tokens.create(db_session, org_id=arbre["m"].id, name="v", role="viewer", created_by=None)
    assert anonymous_client.get(f"/api/v1/orgs/{arbre['s'].slug}/budget", headers={"Authorization": f"Bearer {viewer}"}).status_code == 403


def test_metriques_budget(anonymous_client, db_session, arbre, spend):
    budget.set_cap(db_session, org_id=arbre["m"].id, cap_eur=Decimal("10"), updated_by=None)
    spend(arbre["s"], 4)
    body = anonymous_client.get("/metrics").text
    assert f'geoeval_budget_ratio{{org="{arbre["m"].slug}",period="month"}} 0.4' in body
    assert f'geoeval_budget_spent_eur{{org="{arbre["m"].slug}",period="month"}} 4.0' in body
