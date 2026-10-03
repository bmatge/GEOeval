"""Profils OIDC et correspondance des claims (E6, préparatifs ProConnect) — sans base."""
from __future__ import annotations

import pytest

from geoeval.web import oidc

_VARS = ("OIDC_PROFILE", "OIDC_SCOPES", "OIDC_PROVIDER_LABEL", "OIDC_TRUST_EMAIL", "OIDC_CLAIM_EMAIL",
         "OIDC_CLAIM_GIVEN_NAME", "OIDC_CLAIM_FAMILY_NAME", "OIDC_CLAIM_SIRET", "OIDC_CLAIM_IDP")


@pytest.fixture(autouse=True)
def env_propre(monkeypatch):
    for v in _VARS:
        monkeypatch.delenv(v, raising=False)


def test_profil_generic_par_defaut():
    assert oidc.profile() == "generic"
    assert oidc.provider_label() == "Authentik" and oidc.scopes() == "openid email profile"
    assert oidc.trust_email() is False


def test_profil_proconnect(monkeypatch):
    monkeypatch.setenv("OIDC_PROFILE", "ProConnect")
    assert oidc.profile() == "proconnect" and oidc.provider_label() == "ProConnect"
    assert "siret" in oidc.scopes().split() and "groups" not in oidc.scopes()
    assert oidc.trust_email() is True


def test_variable_vide_vaut_non_posee_et_variable_prioritaire(monkeypatch):
    monkeypatch.setenv("OIDC_PROFILE", "proconnect")
    monkeypatch.setenv("OIDC_SCOPES", "")          # compose : ${OIDC_SCOPES:-}
    monkeypatch.setenv("OIDC_TRUST_EMAIL", "0")
    monkeypatch.setenv("OIDC_PROVIDER_LABEL", "ProConnect (intégration)")
    assert oidc.scopes() == oidc.PROFILES["proconnect"]["scopes"]
    assert oidc.trust_email() is False
    assert oidc.provider_label() == "ProConnect (intégration)"


def test_profil_inconnu_retombe_sur_generic(monkeypatch, caplog):
    monkeypatch.setenv("OIDC_PROFILE", "franceconnect")
    assert oidc.profile() == "generic"


@pytest.mark.parametrize("verified,attested", [(True, True), ("true", True), (False, False), (None, False)])
def test_email_atteste_generic(verified, attested):
    claims = {"sub": "abc", "email": " Agent@Exemple.GOUV.fr "}
    if verified is not None:
        claims["email_verified"] = verified
    ident = oidc.identity_from_claims(claims, issuer_url="https://idp")
    assert ident.email == "agent@exemple.gouv.fr" and ident.email_attested is attested


def test_claims_proconnect(monkeypatch):
    monkeypatch.setenv("OIDC_PROFILE", "proconnect")
    claims = {"sub": "pc-123", "email": "a.b@interieur.gouv.fr", "given_name": "Alice", "usual_name": "Bernard",
              "siret": "11000201100044", "idp_id": "fia1v2", "uid": "u-1"}
    ident = oidc.identity_from_claims(claims, issuer_url="https://proconnect")
    assert (ident.issuer, ident.sub, ident.email) == ("https://proconnect", "pc-123", "a.b@interieur.gouv.fr")
    assert ident.email_attested, "fournisseur de confiance : pas besoin d'email_verified"
    assert (ident.given_name, ident.family_name, ident.siret, ident.idp_id) == ("Alice", "Bernard", "11000201100044", "fia1v2")


def test_correspondance_personnalisee(monkeypatch):
    monkeypatch.setenv("OIDC_CLAIM_EMAIL", "mail")
    monkeypatch.setenv("OIDC_CLAIM_SIRET", "org_siret")
    ident = oidc.identity_from_claims({"sub": "x", "mail": "x@y.fr", "email_verified": True, "org_siret": "123"},
                                      issuer_url="i")
    assert ident.email == "x@y.fr" and ident.siret == "123"


def test_sans_email_ni_sub():
    ident = oidc.identity_from_claims({"email_verified": True}, issuer_url="i")
    assert ident.sub == "" and ident.email is None and ident.email_attested is False
