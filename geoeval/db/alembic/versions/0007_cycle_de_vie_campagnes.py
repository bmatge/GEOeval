"""Cycle de vie des questions et campagnes (ADR-089 §2.9, chantier E8).

Révision : 0007
Précédente : 0006

Arbitrages : brouillon → publiée → retirée ; campagnes à participants désignés,
exécutées par le planificateur ; protocole figé à l'activation ; comparaison
participants × IA. Données existantes : une question active devient « publiée »,
une question désactivée « retirée » (validity_end_at inchangé).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE tests ADD COLUMN status TEXT NOT NULL DEFAULT 'published'")
    op.execute("UPDATE tests SET status = 'retired' WHERE validity_end_at IS NOT NULL")
    op.execute("ALTER TABLE tests ADD CONSTRAINT ck_tests_status CHECK (status IN ('draft', 'published', 'retired'))")
    op.execute("""
        CREATE TABLE campaigns (
            id              SERIAL PRIMARY KEY,
            owner_org_id    INTEGER     NOT NULL REFERENCES organizations(id),
            name            TEXT        NOT NULL,
            description     TEXT,
            status          TEXT        NOT NULL DEFAULT 'draft',
            source_pool_id  INTEGER     REFERENCES question_pools(id) ON DELETE SET NULL,
            tested_models   JSONB       NOT NULL DEFAULT '[]'::jsonb,
            judges          JSONB       NOT NULL DEFAULT '[]'::jsonb,
            protocol        JSONB,
            schedule_kind   TEXT        NOT NULL,
            schedule_config JSONB       NOT NULL,
            next_run_at     TIMESTAMPTZ,
            last_run_at     TIMESTAMPTZ,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_by      INTEGER     REFERENCES users(id),
            activated_at    TIMESTAMPTZ,
            closed_at       TIMESTAMPTZ,
            CONSTRAINT ck_campaigns_status CHECK (status IN ('draft', 'active', 'closed'))
        )
    """)
    op.execute("CREATE INDEX ix_campaigns_owner ON campaigns (owner_org_id)")
    op.execute("CREATE INDEX ix_campaigns_due ON campaigns (status, next_run_at)")
    op.execute("""
        CREATE TABLE campaign_participants (
            campaign_id      INTEGER     NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
            organization_id  INTEGER     NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            added_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_job_id      TEXT,
            last_run_at      TIMESTAMPTZ,
            last_skipped_at  TIMESTAMPTZ,
            last_skip_reason TEXT,
            PRIMARY KEY (campaign_id, organization_id)
        )
    """)
    op.execute("CREATE INDEX ix_campaign_participants_org ON campaign_participants (organization_id)")
    op.execute("ALTER TABLE runs ADD COLUMN campaign_id INTEGER REFERENCES campaigns(id)")
    op.execute("CREATE INDEX ix_runs_campaign_id ON runs (campaign_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_runs_campaign_id")
    op.execute("ALTER TABLE runs DROP COLUMN IF EXISTS campaign_id")
    op.execute("DROP TABLE IF EXISTS campaign_participants")
    op.execute("DROP TABLE IF EXISTS campaigns")
    op.execute("ALTER TABLE tests DROP CONSTRAINT IF EXISTS ck_tests_status")
    op.execute("ALTER TABLE tests DROP COLUMN IF EXISTS status")
