"""Hiérarchie des entités (E1) — helpers purs, sans base."""
from __future__ import annotations

import pytest

from geoeval.db.models import Organization
from geoeval.web import hierarchy as h


def _org(id_, path, depth, parent_id=None, name=None):
    return Organization(id=id_, name=name or f"o{id_}", slug=f"o{id_}", path=path, depth=depth, parent_id=parent_id)


def test_chemins():
    root = _org(12, "/12/", 0)
    assert h.path_for(None, 12) == "/12/"
    assert h.path_for(root, 45) == "/12/45/"
    assert h.ids_from_path("/12/45/78/") == [12, 45, 78]
    assert h.ancestor_ids(_org(78, "/12/45/78/", 2)) == [12, 45]
    assert h.ancestor_ids(root) == []


def test_ancetre_ou_soi_sans_piege_de_prefixe():
    a = _org(1, "/1/", 0)
    b = _org(12, "/12/", 0)
    child = _org(5, "/1/5/", 1, 1)
    assert h.is_ancestor_or_self(a, child) and h.is_ancestor_or_self(child, child)
    assert not h.is_ancestor_or_self(a, b), "/1/ n'est pas préfixe de /12/"
    assert not h.is_ancestor_or_self(child, a)


@pytest.mark.parametrize("raw,expected", [(None, "autre"), ("", "autre"), (" Ministere ", "ministere"), ("service", "service")])
def test_kind_normalise(raw, expected):
    assert h.normalize_kind(raw) == expected


def test_kind_invalide():
    with pytest.raises(h.HierarchyError):
        h.normalize_kind("prefecture")


def test_siret():
    assert h.normalize_siret("110 002 011 00044") == "11000201100044"
    assert h.normalize_siret("") is None and h.normalize_siret(None) is None
    for bad in ("123", "1100020110004A", "110002011000441"):
        with pytest.raises(h.HierarchyError):
            h.normalize_siret(bad)


def test_tree_ordre_profondeur_puis_nom():
    m = _org(1, "/1/", 0, name="Ministère")
    d2 = _org(3, "/1/3/", 1, 1, name="Zeta direction")
    d1 = _org(2, "/1/2/", 1, 1, name="Alpha direction")
    s1 = _org(4, "/1/2/4/", 2, 2, name="Service")
    other = _org(5, "/5/", 0, name="Autre racine")
    assert [o.id for o in h.tree([s1, d2, other, m, d1])] == [5, 1, 2, 4, 3]


def test_tree_orphelin_devient_racine_affichee():
    child = _org(9, "/8/9/", 1, 8)  # parent hors de la liste fournie
    assert [o.id for o in h.tree([child])] == [9]
