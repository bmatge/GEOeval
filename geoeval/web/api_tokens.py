"""
Jetons d'API par organisation (ADR-088 §2.3, lot 1.3b).

Un jeton porte une organisation et un rôle (viewer / editor / org_admin). Il
est créé par un org_admin, montré **une seule fois** en clair, puis seule son
empreinte SHA-256 est conservée. Révocable, avec expiration optionnelle.

Format du clair : ``geoeval_<prefix>_<secret>`` — le préfixe (8 caractères)
sert à identifier le jeton dans l'UI et les journaux sans le divulguer.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from geoeval.db.models import ApiToken
from geoeval.web.tenancy import ROLES

TOKEN_PREFIX = "geoeval"
PREFIX_LEN = 8


def hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate() -> tuple[str, str]:
    """Renvoie (clair, préfixe)."""
    prefix = secrets.token_hex(PREFIX_LEN // 2)
    secret = secrets.token_urlsafe(32)
    return f"{TOKEN_PREFIX}_{prefix}_{secret}", prefix


def create(
    session: Session,
    *,
    org_id: int,
    name: str,
    role: str,
    created_by: Optional[int],
    expires_in_days: Optional[int] = None,
) -> tuple[ApiToken, str]:
    """Crée un jeton ; renvoie (ligne, clair). Le clair n'est plus jamais disponible."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Le nom du jeton est obligatoire.")
    if role not in ROLES:
        raise ValueError(f"Rôle invalide : {role!r} (attendu : {', '.join(ROLES)}).")
    if expires_in_days is not None and not 1 <= int(expires_in_days) <= 3650:
        raise ValueError("L'expiration doit être comprise entre 1 et 3650 jours.")
    plaintext, prefix = generate()
    token = ApiToken(
        organization_id=org_id,
        name=name,
        role=role,
        prefix=prefix,
        token_hash=hash_token(plaintext),
        created_by=created_by,
        expires_at=(datetime.now(timezone.utc) + timedelta(days=int(expires_in_days))) if expires_in_days else None,
    )
    session.add(token)
    session.commit()
    session.refresh(token)
    return token, plaintext


def list_for_org(session: Session, org_id: int) -> list[ApiToken]:
    return list(
        session.execute(
            select(ApiToken).where(ApiToken.organization_id == org_id).order_by(ApiToken.created_at.desc())
        ).scalars().all()
    )


def get_for_org(session: Session, org_id: int, token_id: int) -> Optional[ApiToken]:
    token = session.get(ApiToken, token_id)
    if token is None or token.organization_id != org_id:
        return None
    return token


def revoke(session: Session, org_id: int, token_id: int) -> ApiToken:
    token = get_for_org(session, org_id, token_id)
    if token is None:
        raise ValueError("Jeton introuvable.")
    if token.revoked_at is None:
        token.revoked_at = datetime.now(timezone.utc)
        session.commit()
    return token


def is_valid(token: ApiToken, *, now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if token.revoked_at is not None:
        return False
    if token.expires_at is not None and token.expires_at <= now:
        return False
    return True


def authenticate(session: Session, plaintext: str) -> Optional[ApiToken]:
    """Jeton actif correspondant au clair, ou None. Met à jour last_used_at."""
    plaintext = (plaintext or "").strip()
    if not plaintext.startswith(TOKEN_PREFIX + "_"):
        return None
    token = session.execute(
        select(ApiToken).where(ApiToken.token_hash == hash_token(plaintext))
    ).scalar_one_or_none()
    if token is None or not is_valid(token):
        return None
    token.last_used_at = datetime.now(timezone.utc)
    session.commit()
    return token
