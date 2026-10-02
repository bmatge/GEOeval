"""
Pilotage programmatique d'Alembic (ADR-088 lot 1.4).

Utilisé par `python -m scripts.migrate` (service `migrate` du compose, Job sur
Nubo), par les tests et par le script init_db déprécié. Pas de fichier .ini
requis à l'exécution : la configuration est construite ici.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from geoeval.db.models import Base

logger = logging.getLogger("geoeval.db.migrate")

ALEMBIC_DIR = Path(__file__).resolve().parent / "alembic"
SEED_SQL = Path(__file__).resolve().parent / "seed.sql"


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL absent.")
    return url


def alembic_config(url: Optional[str] = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", url or database_url())
    cfg.set_main_option("timezone", "UTC")
    return cfg


def head_revision(cfg: Optional[Config] = None) -> str:
    script = ScriptDirectory.from_config(cfg or alembic_config())
    heads = script.get_heads()
    if len(heads) != 1:
        raise RuntimeError(f"Plusieurs têtes Alembic : {heads}")
    return heads[0]


def current_revision(engine: Engine) -> Optional[str]:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def upgrade(url: Optional[str] = None, revision: str = "head") -> None:
    """`alembic upgrade <revision>` (défaut : head)."""
    command.upgrade(alembic_config(url), revision)


def apply_seed(engine: Engine) -> None:
    """Données de démarrage idempotentes (ON CONFLICT DO NOTHING)."""
    raw = engine.raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute(SEED_SQL.read_text(encoding="utf-8"))
        raw.commit()
    finally:
        raw.close()


def schema_drift(engine: Engine) -> list[Any]:
    """Différences entre les modèles ORM et la base migrée (vide = aucune dérive).
    Garde-fou CI : une colonne ajoutée à models.py sans révision Alembic échoue ici."""
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True, "compare_server_default": False})
        return compare_metadata(ctx, Base.metadata)


def wait_for_database(url: str, *, timeout: float = 120.0) -> Engine:
    """Attend que la base réponde (service `migrate` lancé avec la base)."""
    import time

    engine = create_engine(url, pool_pre_ping=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return engine
        except Exception as exc:  # noqa: BLE001
            if time.monotonic() >= deadline:
                raise RuntimeError(f"base injoignable après {timeout}s : {exc}") from exc
            logger.info("en attente de la base (%s)…", type(exc).__name__)
            time.sleep(2)


def migrate(url: Optional[str] = None, *, seed: bool = True, timeout: float = 120.0) -> dict[str, Any]:
    """Séquence complète : attente base → upgrade head → seed. Renvoie un résumé."""
    url = url or database_url()
    engine = wait_for_database(url, timeout=timeout)
    before = current_revision(engine)
    upgrade(url)
    after = current_revision(engine)
    if seed:
        apply_seed(engine)
    drift = schema_drift(engine)
    summary = dict(before=before, after=after, head=head_revision(), seeded=seed, drift=len(drift))
    logger.info("migration : %s → %s (head %s), seed=%s, dérive=%d", before, after, summary["head"], seed, len(drift))
    if drift:
        for d in drift:
            logger.warning("dérive ORM/base : %s", d)
    engine.dispose()
    return summary
