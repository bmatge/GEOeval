"""
Client OIDC générique optionnel (ADR-086 §3, pattern ADR-061).

Désactivé par défaut ; activation 100 % par variables d'environnement, sans
référence en dur à un IdP (Authentik aujourd'hui, ProConnect demain) :

    OIDC_ENABLED=1
    OIDC_ISSUER=https://auth.example.org/application/o/geoeval/
    OIDC_CLIENT_ID=...
    OIDC_CLIENT_SECRET=...
    OIDC_SCOPES="openid email profile"     (défaut)
    OIDC_PROVIDER_LABEL="Authentik"        (défaut — texte du bouton /login)
    OIDC_ADMIN_GROUP=lab-team              (optionnel — claim groups ⇒ admin plateforme)

Flux authorization-code + PKCE via authlib ; découverte `.well-known`.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger("webapp.oidc")

_oauth = None  # registre authlib, construit paresseusement


def oidc_enabled() -> bool:
    return (
        os.environ.get("OIDC_ENABLED", "0").strip() in ("1", "true", "yes")
        and bool(os.environ.get("OIDC_ISSUER", "").strip())
        and bool(os.environ.get("OIDC_CLIENT_ID", "").strip())
    )


def provider_label() -> str:
    return os.environ.get("OIDC_PROVIDER_LABEL", "Authentik").strip() or "Authentik"


def admin_group() -> Optional[str]:
    g = os.environ.get("OIDC_ADMIN_GROUP", "").strip()
    return g or None


def issuer() -> str:
    return os.environ.get("OIDC_ISSUER", "").strip().rstrip("/")


def get_client():
    """Client authlib `oidc` (singleton). À n'appeler que si oidc_enabled()."""
    global _oauth
    if _oauth is None:
        from authlib.integrations.starlette_client import OAuth

        _oauth = OAuth()
        _oauth.register(
            name="oidc",
            server_metadata_url=f"{issuer()}/.well-known/openid-configuration",
            client_id=os.environ["OIDC_CLIENT_ID"],
            client_secret=os.environ.get("OIDC_CLIENT_SECRET", ""),
            client_kwargs={
                "scope": os.environ.get("OIDC_SCOPES", "openid email profile"),
                "code_challenge_method": "S256",
            },
        )
    return _oauth.oidc


def claims_admin(claims: dict[str, Any]) -> bool:
    """Vrai si le claim `groups` contient OIDC_ADMIN_GROUP (promotion uniquement)."""
    group = admin_group()
    if not group:
        return False
    groups = claims.get("groups") or []
    if isinstance(groups, str):
        groups = [groups]
    return group in groups
