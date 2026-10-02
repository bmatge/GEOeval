"""
DÉPRÉCIÉ (ADR-088 lot 1.4) : le schéma est géré par Alembic. Utiliser

    python -m scripts.migrate

Ce script reste pour le dev local : `--drop` supprime toutes les tables puis
rejoue les migrations (⚠️ destructif). Sans option, il délègue à scripts.migrate.
"""
from __future__ import annotations

import sys


def main() -> int:
    from geoeval.db import migrate as m

    if "--drop" in sys.argv[1:]:
        from sqlalchemy import create_engine, text

        from geoeval.db.models import Base

        print("⚠️  DROP de toutes les tables GEOeval (et de alembic_version)...")
        engine = create_engine(m.database_url())
        Base.metadata.drop_all(engine)
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        engine.dispose()
    print("init_db est déprécié : délégation à scripts.migrate (alembic upgrade head + seed).")
    summary = m.migrate()
    print(f"OK. Révision {summary['after']} (head {summary['head']}), dérive ORM/base : {summary['drift']}.")
    return 0 if summary["drift"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
