"""Rejugement versionné et calibration des notateurs (ADR-089 §2.9, suite E8).

Révision : 0009
Précédente : 0008

Arbitrages : les notes rejugées s'affichent en comparaison, les notes d'origine restent
la référence (rien n'est écrasé) ; rejugement lancé par editor+ avec les contrôles
habituels ; gold annoté dans l'application, par entité, hérité vers le bas ; accord
suivi par notateur et par thème, notification sous un seuil hérité.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE gold_annotations ADD COLUMN organization_id INTEGER "
               "REFERENCES organizations(id) ON DELETE CASCADE")
    op.execute("ALTER TABLE gold_annotations ADD COLUMN annotator_user_id INTEGER "
               "REFERENCES users(id) ON DELETE SET NULL")
    op.execute("CREATE INDEX ix_gold_annotations_org ON gold_annotations (organization_id)")
    op.execute("ALTER TABLE detector_settings ADD COLUMN calibration_min_rho NUMERIC(3, 2)")
    op.execute("ALTER TABLE detector_settings ADD CONSTRAINT ck_detector_settings_calibration "
               "CHECK (calibration_min_rho IS NULL OR (calibration_min_rho >= 0 AND calibration_min_rho <= 1))")
    op.execute("""
        CREATE TABLE evaluation_batches (
            id                 SERIAL PRIMARY KEY,
            organization_id    INTEGER       NOT NULL REFERENCES organizations(id),
            label              TEXT,
            run_ids            JSONB         NOT NULL,
            judges             JSONB         NOT NULL,
            response_prompt_id INTEGER       REFERENCES evaluation_prompts(prompt_id),
            citation_prompt_id INTEGER       REFERENCES evaluation_prompts(prompt_id),
            grids              JSONB         NOT NULL DEFAULT '{}'::jsonb,
            estimate_eur       NUMERIC(12, 4),
            job_id             TEXT,
            created_by         INTEGER       REFERENCES users(id) ON DELETE SET NULL,
            created_at         TIMESTAMPTZ   NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_evaluation_batches_org ON evaluation_batches (organization_id, created_at)")
    op.execute("""
        CREATE TABLE rejudge_evaluations (
            batch_id               INTEGER      NOT NULL REFERENCES evaluation_batches(id) ON DELETE CASCADE,
            run_id                 INTEGER      NOT NULL REFERENCES runs(run_id),
            test_id                INTEGER      NOT NULL REFERENCES tests(test_id),
            judge_model_id         INTEGER      NOT NULL REFERENCES models(model_id),
            judge_run_index        INTEGER      NOT NULL,
            response_quality_label TEXT,
            response_quality_score NUMERIC(4, 2),
            citation_quality_label TEXT,
            citation_quality_score NUMERIC(4, 2),
            created_at             TIMESTAMPTZ  NOT NULL DEFAULT now(),
            PRIMARY KEY (batch_id, run_id, test_id, judge_model_id, judge_run_index)
        )
    """)
    op.execute("CREATE INDEX ix_rejudge_evaluations_run_test ON rejudge_evaluations (run_id, test_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS rejudge_evaluations")
    op.execute("DROP TABLE IF EXISTS evaluation_batches")
    op.execute("ALTER TABLE detector_settings DROP CONSTRAINT IF EXISTS ck_detector_settings_calibration")
    op.execute("ALTER TABLE detector_settings DROP COLUMN IF EXISTS calibration_min_rho")
    op.execute("DROP INDEX IF EXISTS ix_gold_annotations_org")
    op.execute("ALTER TABLE gold_annotations DROP COLUMN IF EXISTS annotator_user_id")
    op.execute("ALTER TABLE gold_annotations DROP COLUMN IF EXISTS organization_id")
