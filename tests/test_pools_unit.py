"""Règles pures d'E4 : domaines officiels, visibilité des pools, slugs de thèmes."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from geoeval.web import perimeters, pools, themes


@pytest.mark.parametrize("raw,host", [
    ("https://www.Service-Public.fr/particuliers/vosdroits", "service-public.fr"),
    ("impots.gouv.fr", "impots.gouv.fr"),
    ("http://sub.example.org:8080/x?y=1", "sub.example.org"),
    ("  WWW.ademe.fr  ", "ademe.fr"),
    ("", ""),
])
def test_host_of(raw, host):
    assert perimeters.host_of(raw) == host


def test_normalize_domains_texte_libre():
    raw = "https://www.impots.gouv.fr/accueil, service-public.fr\nservice-public.fr ; ademe.fr"
    assert perimeters.normalize_domains(raw) == ["ademe.fr", "impots.gouv.fr", "service-public.fr"]


def test_normalize_domains_liste_et_vide():
    assert perimeters.normalize_domains(["B.fr", "a.fr", "b.fr"]) == ["a.fr", "b.fr"]
    assert perimeters.normalize_domains(None) == []
    assert perimeters.normalize_domains("  ") == []


@pytest.mark.parametrize("bad", ["pas un domaine", "localhost", "exemple..fr", "-x.fr"])
def test_normalize_domains_refuse(bad):
    with pytest.raises(ValueError):
        perimeters.normalize_domains([bad])


def test_normalize_domains_plafond():
    with pytest.raises(ValueError):
        perimeters.normalize_domains([f"d{i}.fr" for i in range(51)])


def test_is_official_sous_domaines_inclus():
    doms = ["service-public.fr", "gouv.fr"]
    assert perimeters.is_official("https://www.service-public.fr/x", doms)
    assert perimeters.is_official("https://entreprendre.service-public.fr/", doms)
    assert perimeters.is_official("https://impots.gouv.fr", doms)
    assert not perimeters.is_official("https://faux-service-public.fr", doms), "suffixe sans point ≠ sous-domaine"
    assert not perimeters.is_official("https://wikipedia.org", doms)
    assert not perimeters.is_official("https://service-public.fr", [])


def test_official_share():
    urls = ["https://service-public.fr/a", "https://wikipedia.org", "https://www.service-public.fr/b", "https://x.com"]
    assert perimeters.official_share(urls, ["service-public.fr"]) == pytest.approx(0.5)
    assert perimeters.official_share([], ["service-public.fr"]) is None


def _org(i, path):
    return SimpleNamespace(id=i, path=path)


@pytest.mark.parametrize("visibility,viewer,expected", [
    ("private", _org(1, "/1/"), True),          # propriétaire
    ("private", _org(2, "/1/2/"), False),
    ("descendants", _org(2, "/1/2/"), True),
    ("descendants", _org(3, "/1/2/3/"), True),
    ("descendants", _org(9, "/9/"), False),
    ("descendants", _org(11, "/11/"), False),   # "/11/" ne commence pas par "/1/"
    ("all", _org(9, "/9/"), True),
])
def test_visible_to(visibility, viewer, expected):
    owner = _org(1, "/1/")
    pool = SimpleNamespace(visibility=visibility)
    assert pools.visible_to(pool, owner, viewer) is expected


def test_visibilite_ascendante_refusee():
    """Un pool d'un service n'est pas visible de son ministère (partage vers le bas uniquement)."""
    owner = _org(3, "/1/2/3/")
    assert not pools.visible_to(SimpleNamespace(visibility="descendants"), owner, _org(1, "/1/"))


@pytest.mark.parametrize("label,slug", [
    ("Énergie & climat", "energie-climat"),
    ("  Santé publique ", "sante-publique"),
    ("Impôts — particuliers", "impots-particuliers"),
])
def test_slugify(label, slug):
    assert themes.slugify(label) == slug
