"""Contrats LLM et politique de routage (E5) sur PostgreSQL."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select, text

from geoeval.core import llm_clients
from geoeval.db.models import AuditLog, Model, Organization, Perimeter, ScheduledRun, UsageRecord
from geoeval.web import api_tokens, contracts, crypto, launching, pricing, routing, tenancy, usage

pytestmark = pytest.mark.integration

PREFIX = "e5-"
TODAY = date.today()


@pytest.fixture(autouse=True)
def fernet_key(monkeypatch):
    monkeypatch.setenv("GEOEVAL_KEY_SECRET", Fernet.generate_key().decode())
    crypto._fernet.cache_clear()
    yield
    crypto._fernet.cache_clear()


@pytest.fixture()
def clean(db_session):
    def _purge():
        ids = [o.id for o in db_session.execute(select(Organization).where(Organization.slug.like(PREFIX + "%"))).scalars()]
        if ids:
            p = {"ids": ids}
            db_session.execute(text("DELETE FROM usage WHERE organization_id = ANY(:ids) OR contract_id IN "
                                    "(SELECT id FROM llm_contracts WHERE organization_id = ANY(:ids))"), p)
            db_session.execute(text("DELETE FROM job_logs WHERE job_id IN (SELECT id FROM jobs WHERE organization_id = ANY(:ids))"), p)
            for tbl, col in (("jobs", "organization_id"), ("scheduled_runs", "organization_id"), ("llm_contracts", "organization_id"),
                             ("routing_policies", "organization_id"), ("perimeters", "organization_id"), ("budgets", "organization_id"),
                             ("api_tokens", "organization_id"), ("memberships", "org_id"), ("audit_log", "org_id")):
                db_session.execute(text(f"DELETE FROM {tbl} WHERE {col} = ANY(:ids)"), p)
            db_session.execute(text("UPDATE organizations SET parent_id = NULL WHERE id = ANY(:ids)"), p)
            db_session.execute(text("DELETE FROM organizations WHERE id = ANY(:ids)"), p)
        db_session.commit()
    _purge()
    yield
    _purge()


def _mk(db, slug, parent=None, name=None):
    return tenancy.create_org(db, name=name or slug, slug=PREFIX + slug, parent=parent)


def _model(db, name, **where):
    stmt = select(Model).where(Model.model_name == name)
    for k, v in where.items():
        stmt = stmt.where(getattr(Model, k).is_(v))
    m = db.execute(stmt.order_by(Model.model_id)).scalars().first()
    assert m is not None, f"modèle {name} absent du seed"
    return m


@pytest.fixture()
def monde(db_session, clean):
    m = _mk(db_session, "min", name="Ministère E5")
    d = _mk(db_session, "dir", parent=m, name="Direction E5")
    s = _mk(db_session, "svc", parent=d, name="Service E5")
    return dict(m=m, d=d, s=s, mistral=_model(db_session, "mistral"), openrouter=_model(db_session, "openrouter"),
                albert=_model(db_session, "albert", is_judge=True), gemini=_model(db_session, "gemini", is_active=True))


def _contract(db, org, family="mistral", **kw):
    kw.setdefault("label", f"Marché {family} {org.name}")
    kw.setdefault("api_key", f"cle-{org.slug}-{family}")
    return contracts.create(db, org, family=family, **kw)


# ---------------------------------------------------------------------
# Résolution
# ---------------------------------------------------------------------
def test_heritage_et_contrat_le_plus_proche(db_session, monde):
    m, d, s, mistral = monde["m"], monde["d"], monde["s"], monde["mistral"]
    assert contracts.resolve(db_session, s, mistral).billed_to == "platform"
    cm = _contract(db_session, m)
    res = contracts.resolve(db_session, s, mistral)
    assert res.usable and res.contract.id == cm.id and res.owner.id == m.id and res.billed_to == "contract"
    assert contracts.resolve(db_session, s, monde["albert"]).contract is None, "autre famille : pas de contrat"
    cd = _contract(db_session, d)
    assert contracts.resolve(db_session, s, mistral).contract.id == cd.id, "le plus proche gagne"
    # À un même niveau, un contrat restreint au modèle prime sur le contrat de famille.
    cd2 = _contract(db_session, d, label="Spécifique", model_ids=[mistral.model_id])
    assert contracts.resolve(db_session, s, mistral).contract.id == cd2.id
    # Désactiver = échappatoire explicite : la cascade continue.
    contracts.update(db_session, cd2, is_active=False)
    contracts.update(db_session, cd, is_active=False)
    assert contracts.resolve(db_session, s, mistral).contract.id == cm.id
    assert [c.id for c, _ in contracts.list_inherited(db_session, s)] == [cm.id]


def test_contrat_expire_ou_epuise_bloque_sans_repli(db_session, monde):
    m, d, s, mistral = monde["m"], monde["d"], monde["s"], monde["mistral"]
    _contract(db_session, m)  # contrat valide plus haut : NE DOIT PAS servir de repli
    expired = _contract(db_session, d, valid_from=TODAY - timedelta(days=60), valid_to=TODAY - timedelta(days=1))
    res = contracts.resolve(db_session, s, mistral)
    assert not res.usable and res.contract.id == expired.id and "n'est pas en vigueur" in res.blocked
    assert res.billed_to == "platform" and res.contract_id is None
    # Un successeur en vigueur au même niveau débloque.
    succ = _contract(db_session, d, label="Successeur", valid_from=TODAY)
    assert contracts.resolve(db_session, s, mistral).contract.id == succ.id
    # Plafond atteint.
    contracts.update(db_session, succ, cap_eur="1")
    usage.record(db_session, org_id=s.id, model_id=mistral.model_id, run_id=None, kind="tested", billed_to="contract",
                 input_tokens=0, output_tokens=0, cost_usd=Decimal("5"), contract_id=succ.id)
    res = contracts.resolve(db_session, s, mistral)
    assert not res.usable and "plafond" in res.blocked
    assert contracts.resolve(db_session, s, mistral, check_cap=False).usable
    # À venir.
    contracts.update(db_session, succ, is_active=False)
    contracts.update(db_session, expired, is_active=False)
    _contract(db_session, d, label="À venir", valid_from=TODAY + timedelta(days=10))
    assert not contracts.resolve(db_session, s, mistral).usable


def test_regles_d_ecriture(db_session, monde):
    m, mistral, albert = monde["m"], monde["mistral"], monde["albert"]
    c = _contract(db_session, m, valid_from=date(2026, 1, 1), valid_to=date(2026, 6, 30))
    with pytest.raises(contracts.ContractError) as ei:
        _contract(db_session, m, label="Doublon")
    assert ei.value.status == 409
    _contract(db_session, m, label="Successeur", valid_from=date(2026, 7, 1))
    _contract(db_session, m, label="Restreint", model_ids=[mistral.model_id])  # coexiste avec le contrat de famille
    for kw in (dict(model_ids=[albert.model_id]), dict(family="generic"), dict(family="inconnue"),
               dict(valid_from=date(2026, 2, 1), valid_to=date(2026, 1, 1)), dict(label=" "), dict(hosting="mars")):
        with pytest.raises(contracts.ContractError):
            _contract(db_session, m, **{"label": "x", "is_active": False, **kw})
    # Une mise à jour invalide ne laisse rien en base.
    with pytest.raises(contracts.ContractError):
        contracts.update(db_session, c, valid_from=date(2027, 1, 1))
    db_session.refresh(c)
    assert c.valid_from == date(2026, 1, 1)
    # Clé chiffrée, remplaçable, effaçable ; jamais en clair en base.
    assert c.api_key_encrypted and "cle-" not in c.api_key_encrypted
    assert contracts.credentials_for(c)[1] == f"cle-{m.slug}-mistral"
    contracts.update(db_session, c, api_key="nouvelle")
    assert contracts.credentials_for(c)[1] == "nouvelle"
    contracts.update(db_session, c, clear_api_key=True)
    assert c.api_key_encrypted is None
    # Suppression : seulement sans consommation.
    usage.record(db_session, org_id=m.id, model_id=mistral.model_id, run_id=None, kind="tested", billed_to="contract",
                 input_tokens=1, output_tokens=1, contract_id=c.id)
    with pytest.raises(contracts.ContractError) as ei:
        contracts.delete(db_session, c)
    assert ei.value.status == 409


def test_client_llm_utilise_le_contrat_ou_bloque(db_session, monde):
    s, d, orouter = monde["s"], monde["d"], monde["openrouter"]
    c = _contract(db_session, d, family="openrouter", base_url="https://proxy.ministere.example/v1")
    client = llm_clients.client_for_model(orouter, organization_id=s.id)
    assert client.api_key == f"cle-{d.slug}-openrouter" and str(client.base_url).startswith("https://proxy.ministere.example")
    contracts.update(db_session, c, valid_to=TODAY - timedelta(days=1), valid_from=TODAY - timedelta(days=30))
    with pytest.raises(llm_clients.LLMCallError, match="pas de repli"):
        llm_clients.client_for_model(orouter, organization_id=s.id)


def test_imputation_de_l_usage_et_du_devis(db_session, monde):
    s, d, mistral = monde["s"], monde["d"], monde["mistral"]
    c = _contract(db_session, d)
    billing = contracts.billing_for(db_session, s.id, mistral)
    row = usage.record(db_session, org_id=s.id, model_id=mistral.model_id, run_id=None, kind="tested",
                       billed_to=billing.billed_to, contract_id=billing.contract_id, input_tokens=0, output_tokens=0,
                       cost_usd=Decimal("2"))
    assert row.contract_id == c.id and row.billed_to == "contract"
    assert contracts.spent(db_session, c.id) == row.cost_eur
    est = pricing.estimate_scan_cost(db_session, org_id=s.id, tests=[], tested_models=[mistral.model_version], judges=[])
    for line in est["by_model"]:
        assert line["contract_id"] == c.id and line["billed_to"] == "contract"


# ---------------------------------------------------------------------
# Politique de routage
# ---------------------------------------------------------------------
def test_politique_restrictive_heritee(db_session, monde):
    m, d, s = monde["m"], monde["d"], monde["s"]
    routing.set_policy(db_session, m, allowed_families=["mistral", "albert", "openrouter"], sovereign_only=False, eu_only=True)
    routing.set_policy(db_session, s, allowed_families=["albert", "openrouter", "gemini"], sovereign_only=True, eu_only=False)
    eff = routing.effective(db_session, s)
    assert eff.allowed_families == {"albert", "openrouter"}, "intersection : gemini ne revient pas"
    assert eff.sovereign_only and eff.sovereign_from.id == s.id and eff.eu_only and eff.eu_from.id == m.id
    assert routing.effective(db_session, d).allowed_families == {"mistral", "albert", "openrouter"}
    # Une politique vide supprime la ligne propre (l'héritage reste).
    assert routing.set_policy(db_session, s, allowed_families=None, sovereign_only=False, eu_only=False) is None
    assert routing.get_own(db_session, s.id) is None and routing.effective(db_session, s).eu_only
    with pytest.raises(contracts.ContractError):
        routing.set_policy(db_session, s, allowed_families=["skynet"], sovereign_only=False, eu_only=False)


def test_violations_ia_evaluees_et_notateurs(db_session, monde):
    m, s, mistral, albert, orouter = monde["m"], monde["s"], monde["mistral"], monde["albert"], monde["openrouter"]
    routing.set_policy(db_session, m, allowed_families=["mistral", "albert"], sovereign_only=False, eu_only=True)
    probs = routing.violations(db_session, s, tested=[orouter], judges=[albert])
    assert len(probs) == 1 and orouter.model_version in probs[0], "fournisseur interdit, même pour une IA évaluée"
    # UE obligatoire : vise les notateurs seulement ; hébergement inconnu refusé.
    assert routing.violations(db_session, s, tested=[mistral], judges=[albert]) == []
    probs = routing.violations(db_session, s, tested=[], judges=[mistral])
    assert probs and "UE" in probs[0]
    # Un contrat qui déclare un hébergement UE rend le notateur conforme.
    _contract(db_session, s, hosting="eu")
    assert routing.violations(db_session, s, tested=[], judges=[mistral]) == []
    routing.set_policy(db_session, s, allowed_families=None, sovereign_only=True, eu_only=False)
    probs = routing.violations(db_session, s, tested=[mistral], judges=[mistral, albert])
    assert len(probs) == 1 and "non souverain" in probs[0]
    kept = routing.filter_models(db_session, s, [mistral, albert, orouter], as_judges=True)
    assert [x.model_id for x in kept] == [albert.model_id]
    assert [x.model_id for x in routing.filter_models(db_session, s, [mistral, albert, orouter], as_judges=False)] == \
        [mistral.model_id, albert.model_id]


# ---------------------------------------------------------------------
# Lancement et planificateur
# ---------------------------------------------------------------------
def _params(tested, judge):
    return dict(tested_models=[tested.model_version], judges=[{"model": judge.model_version, "repeats": 1}], test_ids=None)


def test_conformite_au_lancement(db_session, monde, monkeypatch):
    m, s, mistral, albert = monde["m"], monde["s"], monde["mistral"], monde["albert"]
    routing.set_policy(db_session, m, allowed_families=["albert"], sovereign_only=False, eu_only=False)
    with pytest.raises(launching.LaunchError) as ei:
        launching.estimate_and_check_budget(db_session, s.id, _params(mistral, albert))
    assert ei.value.kind == "routing" and ei.value.status == 403
    routing.set_policy(db_session, m, allowed_families=None, sovereign_only=False, eu_only=False)
    c = _contract(db_session, m, valid_to=TODAY - timedelta(days=1), valid_from=TODAY - timedelta(days=9))
    with pytest.raises(launching.LaunchError) as ei:
        launching.estimate_and_check_budget(db_session, s.id, _params(mistral, albert))
    assert ei.value.kind == "contract" and ei.value.status == 409 and "Marché mistral" in ei.value.detail
    # Plafond du contrat : le devis imputé doit tenir sous le reste.
    contracts.update(db_session, c, valid_to=None, cap_eur="10")
    fake = {"total_eur": Decimal("12"), "by_model": [
        {"model_version": mistral.model_version, "contract_id": c.id, "cost_eur": Decimal("12")}], "unpriced": []}
    monkeypatch.setattr(pricing, "estimate_scan_cost", lambda *a, **k: fake)
    with pytest.raises(launching.LaunchError) as ei:
        launching.estimate_and_check_budget(db_session, s.id, _params(mistral, albert))
    assert ei.value.kind == "contract" and "dépasserait son plafond" in ei.value.detail
    fake["by_model"][0]["cost_eur"] = Decimal("4")
    fake["total_eur"] = Decimal("4")
    assert launching.estimate_and_check_budget(db_session, s.id, _params(mistral, albert)) is fake


def test_echeance_sautee_si_contrat_inutilisable(db_session, monde):
    from geoeval.worker import scheduler

    s, mistral, albert = monde["s"], monde["mistral"], monde["albert"]
    _contract(db_session, s, valid_to=TODAY - timedelta(days=1), valid_from=TODAY - timedelta(days=9))
    peri = Perimeter(organization_id=s.id, name="Site E5", slug="site-e5", kind="site")
    db_session.add(peri)
    db_session.commit()
    sr = ScheduledRun(organization_id=s.id, perimeter_id=peri.id, name="Prog E5", tested_models=[mistral.model_version],
                      judges=[{"model": albert.model_version, "repeats": 1}], test_ids=None, schedule_kind="daily",
                      schedule_config={"time": "09:00"}, enabled=True,
                      next_run_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    db_session.add(sr)
    db_session.commit()
    scheduler.tick_if_leader(db_session)
    db_session.expire_all()
    sr = db_session.get(ScheduledRun, sr.schedule_id)
    assert sr.last_skip_reason and "n'est pas en vigueur" in sr.last_skip_reason and sr.last_job_id is None
    assert db_session.execute(select(AuditLog).where(AuditLog.org_id == s.id, AuditLog.action == "skip_contract")).scalars().first()


# ---------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------
def test_ui_contrats_et_politique(client, db_session, monde):
    s, d, mistral = monde["s"], monde["d"], monde["mistral"]
    r = client.post(f"/o/{d.slug}/contracts/new", data={
        "family": "mistral", "label": "Marché UI", "api_key": "secret-ui", "valid_from": TODAY.isoformat(),
        "cap_eur": "250", "hosting": "eu", "is_active": "true",
    })
    assert r.status_code == 303, r.text
    c = db_session.execute(text("SELECT id, api_key_encrypted FROM llm_contracts WHERE label='Marché UI'")).one()
    assert "secret-ui" not in c.api_key_encrypted
    page = client.get(f"/o/{d.slug}/contracts")
    assert page.status_code == 200 and "Marché UI" in page.text and "secret-ui" not in page.text
    page = client.get(f"/o/{s.slug}/contracts")
    assert "Contrats hérités (1)" in page.text and "<td>Direction E5</td>" in page.text
    _contract(db_session, d, family="gemini", label="Marché Gemini")  # IA active : visible dans « Clé utilisée »
    assert "Contrat « Marché Gemini » (hérité de Direction E5)" in client.get(f"/o/{s.slug}/contracts").text
    assert client.get(f"/o/{d.slug}/contracts/{c.id}/edit").status_code == 200
    assert client.get(f"/o/{s.slug}/contracts/{c.id}/edit").status_code == 404, "édition par l'entité porteuse seulement"
    r = client.post(f"/o/{d.slug}/contracts/{c.id}/edit", data={"label": "Marché UI 2", "model_ids": [mistral.model_id],
                                                                 "is_active": "true"})
    assert r.status_code == 303
    db_session.expire_all()
    c2 = contracts.get(db_session, c.id)
    assert c2.label == "Marché UI 2" and c2.model_ids == [mistral.model_id] and c2.api_key_encrypted == c.api_key_encrypted
    assert client.post(f"/o/{d.slug}/contracts/new", data={"family": "mistral", "label": "Doublon", "is_active": "true",
                                                           "model_ids": [mistral.model_id]}).status_code == 409
    r = client.post(f"/o/{d.slug}/routing-policy", data={"restrict_families": "true", "allowed_families": ["albert", "mistral"],
                                                        "eu_only": "true"})
    assert r.status_code == 303
    assert routing.effective(db_session, s).allowed_families == {"albert", "mistral"}
    assert "Albert (Etalab), Mistral AI" in client.get(f"/o/{s.slug}/contracts").text
    assert client.post(f"/o/{d.slug}/contracts/{c.id}/delete").status_code == 303
    assert "Contrats et politique de routage" in client.get(f"/o/{d.slug}/models").text


# ---------------------------------------------------------------------
# API v1
# ---------------------------------------------------------------------
def _bearer(db, org, role):
    _, plain = api_tokens.create(db, org_id=org.id, name=f"tok-{role}", role=role, created_by=None)
    return {"Authorization": f"Bearer {plain}"}


def test_api_contrats_et_politique(client, db_session, monde):
    m, s, gemini = monde["m"], monde["s"], monde["gemini"]
    h_admin_m, h_admin_s = _bearer(db_session, m, "org_admin"), _bearer(db_session, s, "org_admin")
    h_editor_s = _bearer(db_session, s, "editor")
    base_m, base_s = f"/api/v1/orgs/{m.slug}", f"/api/v1/orgs/{s.slug}"

    r = client.post(f"{base_m}/contracts", json={"family": "gemini", "label": "Marché API", "api_key": "k-api",
                                                 "cap_eur": "100", "extra_headers": {"X-Marche": "42"}}, headers=h_admin_m)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["has_api_key"] and "api_key" not in body and "k-api" not in r.text and body["header_names"] == ["X-Marche"]
    assert body["status"] == "active" and body["inherited"] is False
    assert client.get(f"{base_s}/contracts", headers=h_editor_s).status_code == 403
    listed = client.get(f"{base_s}/contracts", headers=h_admin_s).json()
    assert [(x["id"], x["inherited"], x["owner_org_slug"]) for x in listed] == [(body["id"], True, m.slug)]
    res = {x["model_id"]: x for x in client.get(f"{base_s}/contracts/resolution", headers=h_admin_s).json()}
    assert res[gemini.model_id]["source"] == "contract" and res[gemini.model_id]["contract_id"] == body["id"]
    assert client.patch(f"{base_s}/contracts/{body['id']}", json={"label": "x"}, headers=h_admin_s).status_code == 404
    r = client.patch(f"{base_m}/contracts/{body['id']}", json={"valid_to": (TODAY - timedelta(days=1)).isoformat(),
                                                               "valid_from": (TODAY - timedelta(days=5)).isoformat()},
                     headers=h_admin_m)
    assert r.status_code == 200 and r.json()["status"] == "expired"
    res = {x["model_id"]: x for x in client.get(f"{base_s}/contracts/resolution", headers=h_admin_s).json()}
    assert res[gemini.model_id]["source"] == "blocked" and "pas de repli" in res[gemini.model_id]["blocked_reason"]
    r = client.post(f"{base_m}/contracts", json={"family": "generic", "label": "sans url"}, headers=h_admin_m)
    assert r.status_code == 400 and r.headers["content-type"].startswith("application/problem+json")
    assert client.post(f"{base_m}/contracts", json={"family": "skynet", "label": "x"}, headers=h_admin_m).status_code == 422

    # Politique : lecture editor+, écriture org_admin, effective restrictive.
    assert client.put(f"{base_s}/routing-policy", json={"sovereign_only": True}, headers=h_editor_s).status_code == 403
    r = client.put(f"{base_m}/routing-policy", json={"allowed_families": ["albert", "mistral"], "eu_only": True}, headers=h_admin_m)
    assert r.status_code == 200 and r.json()["own"]["allowed_families"] == ["albert", "mistral"]
    r = client.put(f"{base_s}/routing-policy", json={"allowed_families": ["albert", "gemini"]}, headers=h_admin_s)
    pol = client.get(f"{base_s}/routing-policy", headers=h_editor_s).json()
    assert pol["effective_allowed_families"] == ["albert"] and pol["effective_eu_only"] is True
    assert pol["own"] == {"allowed_families": ["albert", "gemini"], "sovereign_only": False, "eu_only": False}

    assert client.delete(f"{base_m}/contracts/{body['id']}", headers=h_admin_m).status_code == 204
    assert db_session.execute(select(UsageRecord).where(UsageRecord.contract_id == body["id"])).first() is None


def test_seed_notateurs_albert_souverains_et_heberges_ue(db_session):
    """Base neuve : la révision 0005 passe avant la seed, qui doit donc poser elle-même
    souveraineté et hébergement des notateurs Albert (régression CI de la PR #56)."""
    rows = db_session.execute(select(Model).where(Model.model_name == "albert")).scalars().all()
    assert rows and all(m.is_sovereign and m.hosting == "eu" for m in rows)
