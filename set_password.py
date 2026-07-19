"""
Définit (ou réinitialise) le mot de passe local d'un utilisateur — bootstrap
sans SMTP (ADR-086 §1). L'utilisateur est créé s'il n'existe pas.

Usage (dans le conteneur ou en dev local) :
    python set_password.py bertrand@matge.com                 # mot de passe généré
    python set_password.py bertrand@matge.com --password xxx  # mot de passe fourni
    python set_password.py bertrand@matge.com --admin         # + admin plateforme
"""
from __future__ import annotations

import argparse
import secrets
import sys
from datetime import datetime, timezone

from sqlalchemy import select

from db import SessionLocal
from models import User
from webapp.auth import hash_password


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email")
    parser.add_argument("--password", help="mot de passe à poser (généré sinon)")
    parser.add_argument(
        "--admin", action="store_true", help="promeut aussi en admin plateforme"
    )
    args = parser.parse_args()

    email = args.email.strip().lower()
    password = args.password or secrets.token_urlsafe(12)
    if len(password) < 12:
        print("Erreur : 12 caractères minimum.", file=sys.stderr)
        return 1

    with SessionLocal() as session:
        user = session.execute(
            select(User).where(User.email == email)
        ).scalar_one_or_none()
        created = user is None
        if user is None:
            now = datetime.now(timezone.utc)
            user = User(email=email, first_seen_at=now, last_seen_at=now)
            session.add(user)
        user.password_hash = hash_password(password)
        if args.admin:
            user.is_platform_admin = True
        session.commit()

    print(f"{'Créé' if created else 'Mis à jour'} : {email}")
    if not args.password:
        print(f"Mot de passe généré : {password}")
    if args.admin:
        print("Rôle admin plateforme : posé")
    return 0


if __name__ == "__main__":
    sys.exit(main())
