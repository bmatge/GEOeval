"""Hiérarchie des entités : parent_id, kind, path, depth, siret (ADR-089 §2.1, chantier E1).

Révision : 0002
Précédente : 0001

Arbitrages : chemin matérialisé sans extension PostgreSQL ; les organisations
existantes deviennent des racines de type « autre » (path = /id/, depth = 0),
à requalifier et rattacher ensuite par un admin plateforme. Aucune donnée
historique n'est modifiée.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE organizations
            ADD COLUMN parent_id INTEGER REFERENCES organizations(id),
            ADD COLUMN kind      TEXT    NOT NULL DEFAULT 'autre',
            ADD COLUMN path      TEXT,
            ADD COLUMN depth     INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN siret     TEXT
    """)
    op.execute("UPDATE organizations SET path = '/' || id::text || '/', depth = 0 WHERE path IS NULL")
    op.execute("ALTER TABLE organizations ALTER COLUMN path SET NOT NULL")
    op.execute("""
        ALTER TABLE organizations
            ADD CONSTRAINT ck_organizations_kind
                CHECK (kind IN ('ministere', 'direction', 'service', 'autre')),
            ADD CONSTRAINT ck_organizations_depth CHECK (depth >= 0),
            ADD CONSTRAINT ck_organizations_parent_not_self CHECK (parent_id IS NULL OR parent_id <> id),
            ADD CONSTRAINT ck_organizations_siret CHECK (siret IS NULL OR siret ~ '^[0-9]{14}$')
    """)
    op.execute("CREATE INDEX ix_organizations_parent_id ON organizations (parent_id)")
    op.execute("CREATE INDEX ix_organizations_path ON organizations (path text_pattern_ops)")
    op.execute("CREATE INDEX ix_organizations_siret ON organizations (siret)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_organizations_siret")
    op.execute("DROP INDEX IF EXISTS ix_organizations_path")
    op.execute("DROP INDEX IF EXISTS ix_organizations_parent_id")
    op.execute("""
        ALTER TABLE organizations
            DROP CONSTRAINT IF EXISTS ck_organizations_siret,
            DROP CONSTRAINT IF EXISTS ck_organizations_parent_not_self,
            DROP CONSTRAINT IF EXISTS ck_organizations_depth,
            DROP CONSTRAINT IF EXISTS ck_organizations_kind,
            DROP COLUMN IF EXISTS siret,
            DROP COLUMN IF EXISTS depth,
            DROP COLUMN IF EXISTS path,
            DROP COLUMN IF EXISTS kind,
            DROP COLUMN IF EXISTS parent_id
    """)
