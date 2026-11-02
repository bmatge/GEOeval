"""Anti open-redirect : `_safe_next` ne suit que des chemins internes.

CodeQL remonte `py/url-redirection` sur les redirections d'authentification ;
ce test fige le contrat de l'assainisseur, bypass par antislash compris.
"""
from __future__ import annotations

import pytest

from geoeval.web.auth_routes import _safe_next


@pytest.mark.parametrize(
    "chemin",
    ["/", "/dashboard", "/o/ministere/runs/12", "/login?next=%2Fx"],
)
def test_chemins_internes_conserves(chemin):
    assert _safe_next(chemin) == chemin


@pytest.mark.parametrize(
    "hostile",
    [
        None,
        "",
        "https://exemple.com",
        "//exemple.com",
        "/\\exemple.com",       # les navigateurs normalisent \ en / : vaut //exemple.com
        "/\\\\exemple.com",
        "javascript:alert(1)",
        "/ok\r\nLocation: https://exemple.com",
        "/ok\nSet-Cookie: a=b",
    ],
)
def test_redirections_externes_refusees(hostile):
    assert _safe_next(hostile) == "/"
