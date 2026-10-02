"""
Crée le schéma de la base GEOeval (toutes les tables définies dans models.py).

Usage:
    python -m scripts.init_db            # crée les tables manquantes
    python -m scripts.init_db --drop     # DROP puis recrée tout (⚠️ destructif)

La connexion utilise DATABASE_URL (voir .env / .env.example).
Aucune donnée n'est insérée ici : voir geoeval/db/seed.sql pour le peuplement.
"""
from __future__ import annotations

import sys

from geoeval.db.session import engine
from geoeval.db.models import Base


def main() -> None:
    drop = "--drop" in sys.argv[1:]

    if drop:
        print("⚠️  DROP de toutes les tables GEOeval...")
        Base.metadata.drop_all(engine)

    print("Création du schéma (create_all)...")
    Base.metadata.create_all(engine)

    tables = ", ".join(sorted(Base.metadata.tables))
    print(f"OK. Tables présentes : {tables}")


if __name__ == "__main__":
    main()
