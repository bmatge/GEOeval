"""
Migration de la base GEOeval (ADR-088 lot 1.4) — à exécuter AVANT le web et le worker.

    python -m scripts.migrate            # attente base → alembic upgrade head → seed idempotente
    python -m scripts.migrate --no-seed  # sans données de démarrage
    python -m scripts.migrate --check    # état : révision courante vs head, dérive ORM/base (exit 1 si retard ou dérive)

Service `migrate` du compose (one-shot), Job ou initContainer sur Nubo. Code de
sortie non nul en cas d'échec : le déploiement s'arrête là, web et worker ne
démarrent pas sur un schéma incomplet.
"""
from __future__ import annotations

import argparse
import logging
import sys

from geoeval.db import migrate as m
from geoeval.observability.logs import configure_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrations Alembic GEOeval")
    parser.add_argument("--no-seed", action="store_true", help="ne pas appliquer seed.sql")
    parser.add_argument("--check", action="store_true", help="vérifier sans modifier")
    parser.add_argument("--timeout", type=float, default=120.0, help="attente max de la base (s)")
    args = parser.parse_args(argv)
    configure_logging("migrate")
    log = logging.getLogger("scripts.migrate")

    if args.check:
        engine = m.wait_for_database(m.database_url(), timeout=args.timeout)
        current, head = m.current_revision(engine), m.head_revision()
        drift = m.schema_drift(engine) if current == head else []
        log.info("révision courante=%s head=%s dérive=%d", current, head, len(drift))
        for d in drift:
            log.warning("dérive ORM/base : %s", d)
        return 0 if (current == head and not drift) else 1

    try:
        summary = m.migrate(seed=not args.no_seed, timeout=args.timeout)
    except Exception:  # noqa: BLE001
        log.exception("migration en échec")
        return 1
    return 0 if summary["drift"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
