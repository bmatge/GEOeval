"""Règles de rôles et validation de slug (webapp/tenancy.py), sans base."""
from __future__ import annotations

import pytest

from webapp.tenancy import ROLES, create_org, role_at_least


def test_hierarchie_des_roles():
    # ROLES est déclaré du plus fort au plus faible : org_admin > editor > viewer.
    assert ROLES == ("org_admin", "editor", "viewer")
    strongest_first = list(ROLES)
    for i, role in enumerate(strongest_first):
        for weaker in strongest_first[i:]:
            assert role_at_least(role, weaker), f"{role} doit couvrir {weaker}"
        for stronger in strongest_first[:i]:
            assert not role_at_least(role, stronger), f"{role} ne doit pas couvrir {stronger}"


def test_role_absent_ou_inconnu():
    assert not role_at_least(None, ROLES[0])
    assert not role_at_least("inconnu", ROLES[0])


@pytest.mark.parametrize("slug", ["Majuscule", "a", "avec espace", "-debut", "x" * 65, "accent-é"])
def test_slug_invalide_rejete_avant_la_base(slug):
    with pytest.raises(ValueError):
        create_org(None, name="x", slug=slug)  # session None : le regex doit refuser avant
