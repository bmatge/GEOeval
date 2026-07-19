"""
Services de comptes locaux (ADR-086) : authentification par mot de passe,
liens one-shot de définition de mot de passe, administration des utilisateurs.

Séparé de `webapp/auth.py` (résolution d'identité par requête) et de
`webapp/tenancy.py` (orgs/memberships/invitations).
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import AuthToken, User
from webapp.auth import hash_password, verify_password

AUTH_TOKEN_TTL = timedelta(hours=48)


def get_user_by_email(session: Session, email: str) -> Optional[User]:
    return session.execute(
        select(User).where(User.email == email.strip().lower())
    ).scalar_one_or_none()


def authenticate(session: Session, *, email: str, password: str) -> Optional[User]:
    """Vérifie email + mot de passe. None si échec (compte inconnu, sans mot de
    passe local, ou mot de passe faux) — sans distinguer les cas (anti-énumération)."""
    user = get_user_by_email(session, email)
    if user is None or not verify_password(password, user.password_hash):
        return None
    return user


def create_local_user(session: Session, *, email: str, password: str) -> User:
    """Crée un compte local (invitation acceptée par un email inconnu)."""
    now = datetime.now(timezone.utc)
    user = User(
        email=email.strip().lower(),
        first_seen_at=now,
        last_seen_at=now,
        password_hash=hash_password(password),
        auth_provider="local",
    )
    session.add(user)
    session.commit()
    return user


def set_password(session: Session, *, user_id: int, password: str) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise ValueError(f"user {user_id} introuvable")
    user.password_hash = hash_password(password)
    session.commit()
    return user


def validate_password_strength(password: str) -> Optional[str]:
    """Renvoie un message d'erreur ou None si acceptable (POC : longueur seule)."""
    if len(password) < 12:
        return "Le mot de passe doit faire au moins 12 caractères."
    return None


# ---------------------------------------------------------------------
# Liens one-shot de définition de mot de passe (sans SMTP — ADR-086 §1)
# ---------------------------------------------------------------------
def create_set_password_token(session: Session, *, user_id: int) -> AuthToken:
    tok = AuthToken(
        user_id=user_id,
        purpose="set_password",
        token=secrets.token_urlsafe(32),
    )
    session.add(tok)
    session.commit()
    return tok


def get_valid_token(session: Session, token: str) -> Optional[AuthToken]:
    row = session.execute(
        select(AuthToken).where(AuthToken.token == token)
    ).scalar_one_or_none()
    if row is None or row.used_at is not None:
        return None
    if datetime.now(timezone.utc) - row.created_at > AUTH_TOKEN_TTL:
        return None
    return row


def consume_token(session: Session, token_row: AuthToken) -> None:
    token_row.used_at = datetime.now(timezone.utc)
    session.commit()


# ---------------------------------------------------------------------
# Administration plateforme (/admin/users)
# ---------------------------------------------------------------------
def list_users(session: Session) -> list[User]:
    return list(
        session.execute(select(User).order_by(User.email)).scalars().all()
    )


def count_platform_admins(session: Session) -> int:
    return len(
        session.execute(
            select(User.id).where(User.is_platform_admin.is_(True))
        ).all()
    )


def set_platform_admin(
    session: Session, *, user_id: int, value: bool, acting_user_id: int
) -> User:
    """Pose/retire le rôle plateforme. Garde anti-lockout : impossible de
    rétrograder le dernier admin (soi-même compris)."""
    user = session.get(User, user_id)
    if user is None:
        raise ValueError(f"user {user_id} introuvable")
    if not value and user.is_platform_admin and count_platform_admins(session) <= 1:
        raise ValueError("Impossible de rétrograder le dernier admin plateforme.")
    user.is_platform_admin = value
    session.commit()
    return user
