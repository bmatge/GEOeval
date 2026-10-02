"""Base convergente : schéma GEOeval au lot 1.4.

Révision : 0001
Précédente : aucune

Deux chemins, un seul état d'arrivée (arbitrage ADR-088 lot 1.4) :
- base vierge (pas de table `organizations`) : joue `schema_base.sql`, l'instantané
  figé du schéma courant ;
- base existante (VibeLab, bases de dev) : rejoue `migrations.sql`, le script
  idempotent historique qui amène n'importe quelle version antérieure à ce même état.

Aucun `alembic stamp` manuel nulle part. À partir d'ici, migrations.sql est gelé et
toute évolution du schéma est une révision Alembic.
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_HERE = Path(__file__).resolve()
SCHEMA_BASE_SQL = _HERE.parents[1] / "schema_base.sql"
LEGACY_MIGRATIONS_SQL = _HERE.parents[2] / "migrations.sql"


def _exec_sql_file(path: Path) -> None:
    sql = path.read_text(encoding="utf-8")
    op.get_bind().exec_driver_sql(sql)


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "organizations" in existing:
        _exec_sql_file(LEGACY_MIGRATIONS_SQL)
    else:
        _exec_sql_file(SCHEMA_BASE_SQL)


def downgrade() -> None:
    raise NotImplementedError("La révision de base ne se rétrograde pas (elle représente tout le schéma).")
