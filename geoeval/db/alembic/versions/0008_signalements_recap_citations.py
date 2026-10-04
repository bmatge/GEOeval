"""Signalements, récapitulatif quotidien, chute des citations officielles (ADR-089 §2.8, suite E7).

Révision : 0008
Précédente : 0007

Arbitrages : tout membre signale, le propriétaire traite (corrigé / rejeté, avec
réponse) ; préférence d'email à trois choix (immédiat, récapitulatif, aucun) ;
chute des citations mesurée en points sous la moyenne des 3 runs précédents.
Données : email=true → mode 'immediate', email=false → 'none'.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE notification_preferences ADD COLUMN mode TEXT")
    op.execute("UPDATE notification_preferences SET mode = CASE WHEN email THEN 'immediate' ELSE 'none' END")
    op.execute("ALTER TABLE notification_preferences ALTER COLUMN mode SET NOT NULL")
    op.execute("ALTER TABLE notification_preferences DROP COLUMN email")
    op.execute("ALTER TABLE notification_preferences ADD CONSTRAINT ck_notification_preferences_mode "
               "CHECK (mode IN ('immediate', 'digest', 'none'))")
    op.execute("CREATE INDEX ix_notifications_digest_pending ON notifications (user_id, created_at) "
               "WHERE email_status = 'digest_pending'")
    op.execute("ALTER TABLE detector_settings ADD COLUMN citation_drop_points NUMERIC(5, 2)")
    op.execute("ALTER TABLE detector_settings ADD CONSTRAINT ck_detector_settings_citation_drop "
               "CHECK (citation_drop_points IS NULL OR (citation_drop_points > 0 AND citation_drop_points <= 100))")
    op.execute("""
        CREATE TABLE test_reports (
            id                 SERIAL PRIMARY KEY,
            test_id            INTEGER     NOT NULL REFERENCES tests(test_id),
            owner_org_id       INTEGER     NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            reporter_org_id    INTEGER     NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            reporter_user_id   INTEGER     REFERENCES users(id) ON DELETE SET NULL,
            run_id             INTEGER     REFERENCES runs(run_id),
            category           TEXT        NOT NULL,
            comment            TEXT        NOT NULL DEFAULT '',
            status             TEXT        NOT NULL DEFAULT 'open',
            resolution_comment TEXT,
            resolved_by        INTEGER     REFERENCES users(id) ON DELETE SET NULL,
            resolved_at        TIMESTAMPTZ,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_test_reports_category CHECK (category IN ('expected_answer', 'ambiguous', 'obsolete', 'citation', 'other')),
            CONSTRAINT ck_test_reports_status CHECK (status IN ('open', 'fixed', 'rejected'))
        )
    """)
    op.execute("CREATE INDEX ix_test_reports_owner_status ON test_reports (owner_org_id, status)")
    op.execute("CREATE INDEX ix_test_reports_test ON test_reports (test_id)")
    op.execute("CREATE INDEX ix_test_reports_reporter_org ON test_reports (reporter_org_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS test_reports")
    op.execute("ALTER TABLE detector_settings DROP CONSTRAINT IF EXISTS ck_detector_settings_citation_drop")
    op.execute("ALTER TABLE detector_settings DROP COLUMN IF EXISTS citation_drop_points")
    op.execute("DROP INDEX IF EXISTS ix_notifications_digest_pending")
    op.execute("ALTER TABLE notification_preferences DROP CONSTRAINT IF EXISTS ck_notification_preferences_mode")
    op.execute("ALTER TABLE notification_preferences ADD COLUMN email BOOLEAN")
    op.execute("UPDATE notification_preferences SET email = (mode = 'immediate')")
    op.execute("ALTER TABLE notification_preferences ALTER COLUMN email SET NOT NULL")
    op.execute("ALTER TABLE notification_preferences DROP COLUMN mode")
