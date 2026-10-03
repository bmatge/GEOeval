"""Pools de questions, thèmes, domaines des sites (ADR-089 §2.5, chantier E4).

Révision : 0004
Précédente : 0003

Arbitrages : un pool s'exécute en l'abonnant à un périmètre (perimeter_pools) ;
les thèmes forment un catalogue global géré par l'administration plateforme.
Aucune donnée existante n'est modifiée ; perimeters.domains part vide.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE perimeters ADD COLUMN domains JSONB NOT NULL DEFAULT '[]'::jsonb")
    op.execute("""
        CREATE TABLE themes (
            id         SERIAL PRIMARY KEY,
            slug       TEXT        NOT NULL UNIQUE,
            label      TEXT        NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE question_pools (
            id           SERIAL PRIMARY KEY,
            owner_org_id INTEGER     NOT NULL REFERENCES organizations(id),
            name         TEXT        NOT NULL,
            description  TEXT,
            visibility   TEXT        NOT NULL DEFAULT 'private',
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_by   INTEGER     REFERENCES users(id),
            CONSTRAINT ck_question_pools_visibility CHECK (visibility IN ('private', 'descendants', 'all')),
            CONSTRAINT uq_question_pools_owner_name UNIQUE (owner_org_id, name)
        )
    """)
    op.execute("CREATE INDEX ix_question_pools_owner_org_id ON question_pools (owner_org_id)")
    op.execute("""
        CREATE TABLE pool_tests (
            pool_id    INTEGER     NOT NULL REFERENCES question_pools(id) ON DELETE CASCADE,
            test_id    INTEGER     NOT NULL REFERENCES tests(test_id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (pool_id, test_id)
        )
    """)
    op.execute("CREATE INDEX ix_pool_tests_test_id ON pool_tests (test_id)")
    op.execute("""
        CREATE TABLE pool_includes (
            parent_pool_id INTEGER NOT NULL REFERENCES question_pools(id) ON DELETE CASCADE,
            child_pool_id  INTEGER NOT NULL REFERENCES question_pools(id) ON DELETE CASCADE,
            PRIMARY KEY (parent_pool_id, child_pool_id),
            CONSTRAINT ck_pool_includes_not_self CHECK (parent_pool_id <> child_pool_id)
        )
    """)
    op.execute("CREATE INDEX ix_pool_includes_child_pool_id ON pool_includes (child_pool_id)")
    op.execute("""
        CREATE TABLE perimeter_pools (
            perimeter_id INTEGER     NOT NULL REFERENCES perimeters(id) ON DELETE CASCADE,
            pool_id      INTEGER     NOT NULL REFERENCES question_pools(id) ON DELETE CASCADE,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_by   INTEGER     REFERENCES users(id),
            PRIMARY KEY (perimeter_id, pool_id)
        )
    """)
    op.execute("CREATE INDEX ix_perimeter_pools_pool_id ON perimeter_pools (pool_id)")
    op.execute("""
        CREATE TABLE test_themes (
            test_id  INTEGER NOT NULL REFERENCES tests(test_id),
            theme_id INTEGER NOT NULL REFERENCES themes(id) ON DELETE CASCADE,
            PRIMARY KEY (test_id, theme_id)
        )
    """)
    op.execute("CREATE INDEX ix_test_themes_theme_id ON test_themes (theme_id)")
    op.execute("""
        CREATE TABLE perimeter_themes (
            perimeter_id INTEGER NOT NULL REFERENCES perimeters(id) ON DELETE CASCADE,
            theme_id     INTEGER NOT NULL REFERENCES themes(id) ON DELETE CASCADE,
            PRIMARY KEY (perimeter_id, theme_id)
        )
    """)
    op.execute("CREATE INDEX ix_perimeter_themes_theme_id ON perimeter_themes (theme_id)")


def downgrade() -> None:
    for table in ("perimeter_themes", "test_themes", "perimeter_pools", "pool_includes", "pool_tests",
                  "question_pools", "themes"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.execute("ALTER TABLE perimeters DROP COLUMN IF EXISTS domains")
