"""Budget consolidé : alertes de seuil et sauts du planificateur (ADR-089 §2.3, chantier E3).

Révision : 0003
Précédente : 0002

- budget_alerts : une ligne par franchissement de seuil (80 / 100 %) d'un plafond
  (mensuel ou journalier) d'une entité, par période calendaire ; unicité pour ne
  jamais renvoyer la même alerte.
- scheduled_runs.last_skipped_at / last_skip_reason : dernière échéance sautée par
  le planificateur parce qu'elle aurait dépassé un plafond.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE budget_alerts (
            id              SERIAL PRIMARY KEY,
            organization_id INTEGER       NOT NULL REFERENCES organizations(id),
            period          TEXT          NOT NULL,
            period_key      TEXT          NOT NULL,
            threshold       INTEGER       NOT NULL,
            spent_eur       NUMERIC(12,6) NOT NULL,
            cap_eur         NUMERIC(12,2) NOT NULL,
            created_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
            email_status    TEXT          NOT NULL DEFAULT 'pending',
            emailed_to      JSONB,
            CONSTRAINT ck_budget_alerts_period CHECK (period IN ('day', 'month')),
            CONSTRAINT ck_budget_alerts_threshold CHECK (threshold IN (80, 100))
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_budget_alerts_org_period_threshold
            ON budget_alerts (organization_id, period, period_key, threshold)
    """)
    op.execute("ALTER TABLE scheduled_runs ADD COLUMN last_skipped_at TIMESTAMPTZ, ADD COLUMN last_skip_reason TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE scheduled_runs DROP COLUMN IF EXISTS last_skip_reason, DROP COLUMN IF EXISTS last_skipped_at")
    op.execute("DROP TABLE IF EXISTS budget_alerts")
