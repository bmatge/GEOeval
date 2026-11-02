"""Convertit en INTEGER huit colonnes restées BIGINT sur les bases antérieures à Alembic.

Révision : 0011
Précédente : 0010

Les bases créées avant l'adoption d'Alembic (révision 0001 convergente, 2026-10-02)
portent `BIGINT` sur huit colonnes que l'ORM déclare `Integer`. Le contrôle de dérive
du service `migrate` les refuse, et le déploiement s'arrête avant le web et le worker :
c'est ce qui a empêché le redéploiement de `geoeval` le 2026-10-31, après presque
quatre mois sans mise en ligne.

Les bases créées par Alembic n'ont jamais eu ce défaut — `geoeval-beta` déclare bien
`integer` sur les huit colonnes. Cette révision ne change donc rien pour elles, et fait
converger les autres.

**La conversion rétrécit le type**, ce qui n'est sûr que si aucune valeur ne dépasse la
borne d'un entier signé 32 bits. Le `USING` l'impose à PostgreSQL, qui refuserait la
migration plutôt que de tronquer. Mesuré avant écriture sur la base concernée : plus
grand identifiant rencontré, 16.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (table, colonne) — les huit que le contrôle de dérive signale.
COLONNES: tuple[tuple[str, str], ...] = (
    ("api_tokens", "id"),
    ("job_logs", "id"),
    ("runs", "organization_id"),
    ("runs", "perimeter_id"),
    ("scheduled_runs", "organization_id"),
    ("scheduled_runs", "perimeter_id"),
    ("tests", "organization_id"),
    ("tests", "perimeter_id"),
)


def _type_actuel(table: str, colonne: str) -> str:
    return (
        "SELECT data_type FROM information_schema.columns "
        f"WHERE table_name = '{table}' AND column_name = '{colonne}'"
    )


def upgrade() -> None:
    connexion = op.get_bind()
    for table, colonne in COLONNES:
        actuel = connexion.exec_driver_sql(_type_actuel(table, colonne)).scalar()
        # Une base déjà conforme n'est pas touchée : la révision doit pouvoir
        # s'appliquer partout, y compris là où il n'y a rien à convertir.
        if actuel != "bigint":
            continue
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {colonne} "
            f"TYPE INTEGER USING {colonne}::integer"
        )


def downgrade() -> None:
    # Le retour en arrière élargit le type : toujours sûr, aucune valeur ne peut
    # être perdue.
    for table, colonne in COLONNES:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {colonne} "
            f"TYPE BIGINT USING {colonne}::bigint"
        )
