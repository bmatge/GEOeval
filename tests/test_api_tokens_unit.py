"""Jetons d'API — helpers sans base."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from geoeval.db.models import ApiToken
from geoeval.web import api_tokens


def test_generate_format_et_empreinte():
    plain, prefix = api_tokens.generate()
    assert plain.startswith("geoeval_" + prefix + "_") and len(prefix) == 8
    assert api_tokens.hash_token(plain) != plain and len(api_tokens.hash_token(plain)) == 64
    assert api_tokens.generate()[0] != plain


def test_is_valid_revoque_ou_expire():
    now = datetime.now(timezone.utc)
    t = ApiToken(organization_id=1, name="x", role="viewer", prefix="a", token_hash="h")
    assert api_tokens.is_valid(t, now=now)
    t.expires_at = now + timedelta(days=1)
    assert api_tokens.is_valid(t, now=now)
    t.expires_at = now - timedelta(seconds=1)
    assert not api_tokens.is_valid(t, now=now)
    t.expires_at = None
    t.revoked_at = now
    assert not api_tokens.is_valid(t, now=now)


def test_authenticate_rejette_sans_requete_un_format_etranger():
    # Pas de session nécessaire : le préfixe filtre avant toute requête.
    assert api_tokens.authenticate(None, "Bearer n'importe quoi") is None
    assert api_tokens.authenticate(None, "") is None
