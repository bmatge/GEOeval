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
    OIDC_ADMIN_GROUP=lab-team              (TRANSITOIRE Authentik — claim groups ⇒ admin
                                            plateforme ; vide par défaut, à ne pas poser
                                            avec ProConnect, qui n'émet pas de groups)

Profil et correspondance des claims (E6, préparatifs ProConnect) :

    OIDC_PROFILE=generic | proconnect      (défaut generic ; proconnect pose les défauts
                                            ci-dessous, chaque variable reste prioritaire)
    OIDC_TRUST_EMAIL=0|1                   (1 = fournisseur de confiance : l'email vaut
                                            attestation même sans email_verified ;
                                            défaut 0, 1 avec le profil proconnect)
    OIDC_CLAIM_EMAIL=email
    OIDC_CLAIM_GIVEN_NAME=given_name
    OIDC_CLAIM_FAMILY_NAME=family_name     (proconnect : usual_name)
    OIDC_CLAIM_SIRET=siret
    OIDC_CLAIM_IDP=idp_id

Principe (ADR-089) : le fournisseur OIDC authentifie, il n'habilite pas. Les rôles
d'organisation (`memberships`) et le rôle plateforme (`users.is_platform_admin`) vivent
en base et se gèrent dans l'application (invitations, /admin/users). Le bootstrap du
premier admin passe par GEOEVAL_ADMIN_EMAILS.

Flux authorization-code + PKCE via authlib ; découverte `.well-known`.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from geoeval.db.models import User

logger = logging.getLogger("geoeval.web.oidc")

_oauth = None  # registre authlib, construit paresseusement


def oidc_enabled() -> bool:
    return (
        os.environ.get("OIDC_ENABLED", "0").strip() in ("1", "true", "yes")
        and bool(os.environ.get("OIDC_ISSUER", "").strip())
        and bool(os.environ.get("OIDC_CLIENT_ID", "").strip())
    )


# Défauts par profil. Les variables d'environnement restent prioritaires ; une
# variable posée VIDE (compose : ${VAR:-}) vaut « non posée ».
PROFILES: dict[str, dict[str, str]] = {
    "generic": {
        "label": "Authentik",
        "scopes": "openid email profile",
        "trust_email": "0",
        "claim_email": "email",
        "claim_given_name": "given_name",
        "claim_family_name": "family_name",
        "claim_siret": "siret",
        "claim_idp": "idp_id",
    },
    # ProConnect (fédération d'identité des agents publics) : pas de claim groups,
    # nom d'usage dans `usual_name`, SIRET de l'employeur dans `siret`.
    "proconnect": {
        "label": "ProConnect",
        "scopes": "openid email given_name usual_name uid siret idp_id",
        "trust_email": "1",
        "claim_email": "email",
        "claim_given_name": "given_name",
        "claim_family_name": "usual_name",
        "claim_siret": "siret",
        "claim_idp": "idp_id",
    },
}


def profile() -> str:
    name = (os.environ.get("OIDC_PROFILE") or "").strip().lower() or "generic"
    if name not in PROFILES:
        logger.warning("OIDC_PROFILE=%r inconnu : profil generic appliqué", name)
        return "generic"
    return name


def _setting(env: str, key: str) -> str:
    return (os.environ.get(env) or "").strip() or PROFILES[profile()][key]


def provider_label() -> str:
    return _setting("OIDC_PROVIDER_LABEL", "label")


def scopes() -> str:
    return _setting("OIDC_SCOPES", "scopes")


def trust_email() -> bool:
    return _setting("OIDC_TRUST_EMAIL", "trust_email").lower() in ("1", "true", "yes")


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
                # Variable VIDE = non posée (compose : ${OIDC_SCOPES:-}) — sans
                # scope, pas d'id_token et le callback échoue sur le claim sub.
                "scope": scopes(),
                "code_challenge_method": "S256",
            },
        )
    return _oauth.oidc


def claims_admin(claims: dict[str, Any]) -> bool:
    """Vrai si le claim `groups` contient OIDC_ADMIN_GROUP (promotion uniquement).

    Mécanisme de transition pour Authentik : inactif tant que OIDC_ADMIN_GROUP est
    vide (défaut). ProConnect n'émet pas de claim `groups` ; les habilitations
    vivent en base (ADR-089). Journalisé en WARNING à chaque usage effectif.
    """
    group = admin_group()
    if not group:
        return False
    groups = claims.get("groups") or []
    if isinstance(groups, str):
        groups = [groups]
    matched = group in groups
    if matched:
        logger.warning(
            "promotion admin plateforme via le claim OIDC groups=%r (OIDC_ADMIN_GROUP) : "
            "mécanisme transitoire, préférer GEOEVAL_ADMIN_EMAILS puis /admin/users",
            group,
        )
    return matched


# ---------------------------------------------------------------------
# Identité et rattachement (E6, préparatifs ProConnect)
# ---------------------------------------------------------------------
@dataclass
class OidcIdentity:
    """Identité normalisée issue des claims, selon le profil et la correspondance."""
    issuer: str
    sub: str
    email: Optional[str] = None
    email_attested: bool = False        # email_verified, ou fournisseur de confiance
    given_name: Optional[str] = None
    family_name: Optional[str] = None
    siret: Optional[str] = None         # lu, pas encore exploité (E6 complet)
    idp_id: Optional[str] = None


def _claim(claims: dict[str, Any], name: str) -> Optional[str]:
    v = claims.get(name)
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def identity_from_claims(claims: dict[str, Any], *, issuer_url: Optional[str] = None) -> OidcIdentity:
    """Claims → identité. `sub` absent → sub vide (refusé par `resolve_user`)."""
    email = _claim(claims, _setting("OIDC_CLAIM_EMAIL", "claim_email"))
    verified = claims.get("email_verified")
    verified = verified is True or str(verified).lower() == "true"
    return OidcIdentity(
        issuer=(issuer_url if issuer_url is not None else issuer()),
        sub=_claim(claims, "sub") or "",
        email=email.lower() if email else None,
        email_attested=bool(email) and (verified or trust_email()),
        given_name=_claim(claims, _setting("OIDC_CLAIM_GIVEN_NAME", "claim_given_name")),
        family_name=_claim(claims, _setting("OIDC_CLAIM_FAMILY_NAME", "claim_family_name")),
        siret=_claim(claims, _setting("OIDC_CLAIM_SIRET", "claim_siret")),
        idp_id=_claim(claims, _setting("OIDC_CLAIM_IDP", "claim_idp")),
    )


@dataclass
class Resolution:
    """Issue du rattachement : `user` connecté, ou `error` (message affichable).
    `events` : (action d'audit, métadonnées) à journaliser par l'appelant."""
    user: Optional[User] = None
    error: Optional[str] = None
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


def _user_by_email(session: Session, email: str) -> Optional[User]:
    return session.execute(select(User).where(User.email == email)).scalar_one_or_none()


def resolve_user(session: Session, ident: OidcIdentity) -> Resolution:
    """Retrouve ou crée le compte d'une identité OIDC (ADR-089 §2.7). Clé stable :
    (issuer, sub) — l'email change avec les mutations. Commite.

    1. (issuer, sub) connu : connexion. Nouvel email attesté et libre ⇒ mis à jour
       (`oidc_email_updated`) ; déjà pris ⇒ conservé, conflit tracé (`oidc_email_conflict`).
    2. Inconnu : rattachement par email, seulement s'il est attesté (anti-takeover).
       Refusé si le compte est déjà lié à un AUTRE sub chez ce même fournisseur.
       Lié à un autre fournisseur : re-rattachement (migration Authentik → ProConnect).
    3. Aucun compte : création (auth_provider='oidc').
    """
    res = Resolution()
    meta = {"issuer": ident.issuer, "sub": ident.sub}
    if not ident.sub:
        res.error = "Réponse SSO sans identifiant (claim sub)."
        return res
    now = datetime.now(timezone.utc)
    user = session.execute(
        select(User).where(User.oidc_issuer == ident.issuer, User.oidc_external_id == ident.sub)
    ).scalar_one_or_none()

    if user is not None:
        user.last_seen_at = now
        if ident.email and ident.email_attested and ident.email != user.email:
            other = _user_by_email(session, ident.email)
            if other is None:
                res.events.append(("oidc_email_updated", {**meta, "old": user.email, "new": ident.email}))
                user.email = ident.email
            elif other.id != user.id:
                res.events.append(("oidc_email_conflict", {**meta, "kept": user.email, "claimed": ident.email,
                                                           "other_user_id": other.id}))
        session.commit()
        res.user = user
        return res

    if not ident.email or not ident.email_attested:
        res.error = "Connexion SSO refusée : email absent ou non vérifié par le fournisseur d'identité."
        res.events.append(("oidc_rejected", {**meta, "email": ident.email, "reason": "email absent ou non vérifié"}))
        return res

    user = _user_by_email(session, ident.email)
    if user is None:
        user = User(email=ident.email, first_seen_at=now, last_seen_at=now, auth_provider="oidc",
                    oidc_issuer=ident.issuer, oidc_external_id=ident.sub)
        session.add(user)
        session.commit()
        res.events.append(("oidc_created", {**meta, "email": ident.email}))
        res.user = user
        return res

    if user.oidc_issuer == ident.issuer and user.oidc_external_id and user.oidc_external_id != ident.sub:
        res.error = ("Connexion SSO refusée : ce compte est déjà lié à une autre identité chez ce fournisseur. "
                     "Contacte un administrateur.")
        res.events.append(("oidc_rejected", {**meta, "email": ident.email, "user_id": user.id,
                                             "reason": "compte lié à un autre sub du même fournisseur"}))
        return res
    previous = user.oidc_issuer
    user.oidc_issuer = ident.issuer
    user.oidc_external_id = ident.sub
    user.last_seen_at = now
    session.commit()
    res.events.append(("oidc_linked", {**meta, "email": ident.email, "previous_issuer": previous}))
    res.user = user
    return res
