"""Rôles hérités et délégation de structure (E2) — règles pures, sans base."""
from __future__ import annotations

from geoeval.db.models import Organization
from geoeval.web import tenancy as t


def _org(id_, path):
    return Organization(id=id_, name=f"o{id_}", slug=f"o{id_}", path=path, depth=path.strip("/").count("/"))


M, D, S, D2, X = _org(1, "/1/"), _org(2, "/1/2/"), _org(3, "/1/2/3/"), _org(4, "/1/4/"), _org(9, "/9/")


def test_role_herite_vers_le_bas_jamais_vers_le_haut():
    r = t.resolve_role({1: "viewer"}, S)
    assert r == "viewer" and r.anchor_id == 1 and r.anchor_depth == 0
    assert t.resolve_role({3: "editor"}, M) is None
    assert t.resolve_role({2: "org_admin"}, D2) is None, "pas de rôle sur une branche sœur"


def test_role_effectif_est_le_maximum_de_la_chaine():
    r = t.resolve_role({1: "viewer", 3: "editor"}, S)
    assert r == "editor" and r.anchor_id == 3 and r.anchor_depth == 2
    r = t.resolve_role({1: "org_admin", 3: "viewer"}, S)
    assert r == "org_admin" and r.anchor_id == 1


def test_a_role_egal_l_ancre_la_plus_haute():
    r = t.resolve_role({1: "org_admin", 2: "org_admin"}, S)
    assert r.anchor_id == 1 and r.anchor_depth == 0


def test_effective_role_est_une_chaine():
    r = t.EffectiveRole("editor", anchor_id=1, anchor_depth=0)
    assert r == "editor" and isinstance(r, str) and t.role_at_least(r, "viewer") and not t.role_at_least(r, "org_admin")
    assert f"{r}" == "editor"


def test_creation_deleguee():
    assert t.can_create_under({1: "org_admin"}, False, S)
    assert t.can_create_under({1: "org_admin"}, False, M), "sous sa propre entité d'ancrage"
    assert not t.can_create_under({1: "org_admin"}, False, None), "racine : plateforme seulement"
    assert not t.can_create_under({1: "editor"}, False, S)
    assert not t.can_create_under({2: "org_admin"}, False, D2)
    assert t.can_create_under({}, True, None)


def test_qualification_deleguee():
    assert t.can_qualify({1: "org_admin"}, False, S) and t.can_qualify({1: "org_admin"}, False, D)
    assert not t.can_qualify({1: "org_admin"}, False, M), "pas sa propre entité d'ancrage"
    assert not t.can_qualify({2: "org_admin"}, False, D2)


def test_deplacement_delegue_reste_dans_le_perimetre():
    a = {1: "org_admin"}
    assert t.can_restructure(a, False, S, D2), "d'une direction à l'autre du même ministère"
    assert t.can_restructure(a, False, S, M)
    assert not t.can_restructure(a, False, S, None), "pas de racine"
    assert not t.can_restructure(a, False, S, X), "pas hors du périmètre"
    assert not t.can_restructure(a, False, M, D), "pas son entité d'ancrage"
    assert not t.can_restructure({2: "org_admin"}, False, S, D2), "D2 hors du sous-arbre de D"
    assert t.can_restructure({2: "org_admin"}, False, S, D)
    assert t.can_restructure({}, True, M, None)
