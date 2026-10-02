"""Les habilitations vivent dans l'application, pas chez le fournisseur OIDC (ADR-089).

Vérifie que les mécanismes « groupe fournisseur ⇒ admin » restent opt-in et que le
bootstrap passe par GEOEVAL_ADMIN_EMAILS.
"""
from __future__ import annotations

import logging


from geoeval.web import oidc
from geoeval.web.auth import PLATFORM_ADMIN_GROUP, _dev_fake_groups, _parse_groups, bootstrap_admin_emails, proxy_auth_enabled


def test_claim_groups_ignore_sans_config(monkeypatch):
    """Défaut ProConnect : même un claim groups ne promeut personne."""
    monkeypatch.delenv("OIDC_ADMIN_GROUP", raising=False)
    assert oidc.admin_group() is None
    assert oidc.claims_admin({"groups": ["lab-team", "admins"]}) is False


def test_claim_groups_opt_in_authentik(monkeypatch, caplog):
    monkeypatch.setenv("OIDC_ADMIN_GROUP", "lab-team")
    with caplog.at_level(logging.WARNING, logger="geoeval.web.oidc"):
        assert oidc.claims_admin({"groups": ["lab-team"]}) is True
    assert "transitoire" in caplog.text
    assert oidc.claims_admin({"groups": ["autre"]}) is False
    assert oidc.claims_admin({"groups": "lab-team"}) is True  # claim scalaire toléré
    assert oidc.claims_admin({}) is False


def test_claims_proconnect_sans_groups_ne_promeuvent_pas(monkeypatch):
    monkeypatch.setenv("OIDC_ADMIN_GROUP", "lab-team")
    claims = {"sub": "abc", "email": "agent@ministere.gouv.fr", "siret": "11000201100044",
              "given_name": "Jean", "usual_name": "Dupont", "idp_id": "x"}
    assert oidc.claims_admin(claims) is False


def test_proxy_groups_opt_in(monkeypatch):
    monkeypatch.delenv("AUTH_PROXY_ENABLED", raising=False)
    assert proxy_auth_enabled() is False
    monkeypatch.setenv("AUTH_PROXY_ENABLED", "1")
    assert proxy_auth_enabled() is True


def test_parse_groups():
    assert _parse_groups(None) == []
    assert _parse_groups("") == []
    assert _parse_groups(" lab-team , viewers,, ") == ["lab-team", "viewers"]


def test_dev_fake_groups_defaut_admin(monkeypatch):
    monkeypatch.delenv("DEV_FAKE_GROUPS", raising=False)
    assert _dev_fake_groups() == [PLATFORM_ADMIN_GROUP]
    monkeypatch.setenv("DEV_FAKE_GROUPS", "")
    assert _dev_fake_groups() == []


def test_bootstrap_admin_emails_normalises(monkeypatch):
    monkeypatch.setenv("GEOEVAL_ADMIN_EMAILS", " Bertrand@Matge.com, autre@example.org ,")
    assert bootstrap_admin_emails() == {"bertrand@matge.com", "autre@example.org"}
    monkeypatch.setenv("GEOEVAL_ADMIN_EMAILS", "")
    assert bootstrap_admin_emails() == set()
