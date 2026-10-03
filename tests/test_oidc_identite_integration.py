"""Rattachement d'une identité OIDC à un compte (E6, préparatifs ProConnect) sur PostgreSQL :
clé stable (issuer, sub), email qui suit les mutations, anti-takeover, callback simulé."""
from __future__ import annotations

import pytest
from sqlalchemy import select, text

from geoeval.db.models import AuditLog, User
from geoeval.web import oidc

pytestmark = pytest.mark.integration

ISS = "https://idp.e6.test"
DOMAIN = "@e6.test"


@pytest.fixture()
def clean(db_session):
    def _purge():
        db_session.execute(text("DELETE FROM audit_log WHERE user_id IN (SELECT id FROM users WHERE email LIKE :d)"
                                " OR meta_json->>'issuer' LIKE 'https://%e6.test%'"), {"d": "%" + DOMAIN})
        db_session.execute(text("DELETE FROM users WHERE email LIKE :d"), {"d": "%" + DOMAIN})
        db_session.commit()
    _purge()
    yield
    _purge()


def _ident(sub, email=None, attested=True, issuer=ISS):
    return oidc.OidcIdentity(issuer=issuer, sub=sub, email=email, email_attested=attested and bool(email))


def _user(db, email, **kw):
    u = User(email=email, **kw)
    db.add(u)
    db.commit()
    return u


def test_creation_puis_reconnexion_par_sub(db_session, clean):
    r = oidc.resolve_user(db_session, _ident("s1", "alice" + DOMAIN))
    assert r.user is not None and r.user.auth_provider == "oidc" and [e for e, _ in r.events] == ["oidc_created"]
    again = oidc.resolve_user(db_session, _ident("s1", None))
    assert again.user.id == r.user.id and again.events == [], "le sub suffit, l'email n'est plus requis"


def test_l_email_suit_la_mutation_s_il_est_libre(db_session, clean):
    u = _user(db_session, "bob" + DOMAIN, oidc_issuer=ISS, oidc_external_id="s2")
    r = oidc.resolve_user(db_session, _ident("s2", "bob.nouveau" + DOMAIN))
    assert r.user.id == u.id and r.user.email == "bob.nouveau" + DOMAIN
    assert r.events == [("oidc_email_updated", {"issuer": ISS, "sub": "s2", "old": "bob" + DOMAIN,
                                                "new": "bob.nouveau" + DOMAIN})]
    # Email non attesté : ignoré.
    r = oidc.resolve_user(db_session, _ident("s2", "bob.autre" + DOMAIN, attested=False))
    assert r.user.email == "bob.nouveau" + DOMAIN and r.events == []


def test_email_deja_pris_conserve_et_trace(db_session, clean):
    other = _user(db_session, "carole" + DOMAIN)
    u = _user(db_session, "dan" + DOMAIN, oidc_issuer=ISS, oidc_external_id="s3")
    r = oidc.resolve_user(db_session, _ident("s3", "carole" + DOMAIN))
    assert r.user.id == u.id and r.user.email == "dan" + DOMAIN
    assert r.events[0][0] == "oidc_email_conflict" and r.events[0][1]["other_user_id"] == other.id


def test_rattachement_par_email_atteste_seulement(db_session, clean):
    local = _user(db_session, "eve" + DOMAIN, auth_provider="local")
    refused = oidc.resolve_user(db_session, _ident("s4", "eve" + DOMAIN, attested=False))
    assert refused.user is None and "non vérifié" in refused.error and refused.events[0][0] == "oidc_rejected"
    ok = oidc.resolve_user(db_session, _ident("s4", "eve" + DOMAIN))
    assert ok.user.id == local.id and ok.user.oidc_external_id == "s4" and ok.events[0][0] == "oidc_linked"


def test_anti_takeover_meme_fournisseur_autre_sub(db_session, clean):
    victim = _user(db_session, "fanny" + DOMAIN, oidc_issuer=ISS, oidc_external_id="s5")
    r = oidc.resolve_user(db_session, _ident("pirate", "fanny" + DOMAIN))
    assert r.user is None and "déjà lié" in r.error
    db_session.refresh(victim)
    assert victim.oidc_external_id == "s5", "le lien existant n'est jamais écrasé"


def test_migration_vers_un_autre_fournisseur(db_session, clean):
    u = _user(db_session, "gus" + DOMAIN, oidc_issuer="https://authentik.e6.test", oidc_external_id="old")
    r = oidc.resolve_user(db_session, _ident("pc-gus", "gus" + DOMAIN, issuer="https://proconnect.e6.test"))
    assert r.user.id == u.id and (r.user.oidc_issuer, r.user.oidc_external_id) == ("https://proconnect.e6.test", "pc-gus")
    assert r.events[0] == ("oidc_linked", {"issuer": "https://proconnect.e6.test", "sub": "pc-gus",
                                           "email": "gus" + DOMAIN, "previous_issuer": "https://authentik.e6.test"})


def test_sans_sub_refuse(db_session, clean):
    r = oidc.resolve_user(db_session, _ident("", "h" + DOMAIN))
    assert r.user is None and "claim sub" in r.error


class _FakeClient:
    def __init__(self, claims):
        self.claims = claims

    async def authorize_access_token(self, request):
        return {"userinfo": self.claims}


def test_callback_profil_proconnect_simule(anonymous_client, db_session, clean, monkeypatch):
    """Le callback applique profil et correspondance : ProConnect n'envoie pas email_verified."""
    monkeypatch.setenv("OIDC_ENABLED", "1")
    monkeypatch.setenv("OIDC_ISSUER", "https://proconnect.e6.test")
    monkeypatch.setenv("OIDC_CLIENT_ID", "geoeval")
    monkeypatch.setenv("OIDC_PROFILE", "proconnect")
    claims = {"sub": "pc-ines", "email": "ines" + DOMAIN, "given_name": "Inès", "usual_name": "Martin",
              "siret": "11000201100044", "idp_id": "fia1v2"}
    monkeypatch.setattr(oidc, "get_client", lambda: _FakeClient(claims))
    r = anonymous_client.get("/auth/oidc/callback")
    assert r.status_code == 303 and r.headers["location"] == "/"
    u = db_session.execute(select(User).where(User.email == "ines" + DOMAIN)).scalar_one()
    assert (u.oidc_issuer, u.oidc_external_id, u.auth_provider) == ("https://proconnect.e6.test", "pc-ines", "oidc")
    actions = set(db_session.execute(select(AuditLog.action).where(AuditLog.user_id == u.id)).scalars())
    assert {"oidc_created", "login"} <= actions
    # Profil generic : sans email_verified, refusé et tracé.
    monkeypatch.setenv("OIDC_PROFILE", "generic")
    monkeypatch.setattr(oidc, "get_client", lambda: _FakeClient({"sub": "x", "email": "jo" + DOMAIN}))
    r = anonymous_client.get("/auth/oidc/callback")
    assert r.status_code == 200 and "non vérifié" in r.text
    assert db_session.execute(select(User).where(User.email == "jo" + DOMAIN)).first() is None
