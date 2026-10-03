"""Notifications, préférences d'email et réglages des détecteurs (ADR-089 §2.8, chantier E7).

Révision : 0006
Précédente : 0005

Arbitrages : destinataires par rôle selon le type + préférence d'email par
utilisateur ; seuil « question toujours fausse » global, surchargeable par
entité (hérité) ; emails immédiats. Aucune donnée existante n'est modifiée.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE notifications (
            id              SERIAL PRIMARY KEY,
            user_id         INTEGER     NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            organization_id INTEGER     REFERENCES organizations(id) ON DELETE CASCADE,
            kind            TEXT        NOT NULL,
            title           TEXT        NOT NULL,
            body            TEXT        NOT NULL DEFAULT '',
            link            TEXT,
            payload         JSONB,
            dedup_key       TEXT,
            email_status    TEXT        NOT NULL DEFAULT 'none',
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            read_at         TIMESTAMPTZ
        )
    """)
    op.execute("CREATE UNIQUE INDEX uq_notifications_user_dedup ON notifications (user_id, dedup_key) "
               "WHERE dedup_key IS NOT NULL")
    op.execute("CREATE INDEX ix_notifications_user_created ON notifications (user_id, created_at DESC)")
    op.execute("CREATE INDEX ix_notifications_org ON notifications (organization_id)")
    op.execute("""
        CREATE TABLE notification_preferences (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind    TEXT    NOT NULL,
            email   BOOLEAN NOT NULL,
            PRIMARY KEY (user_id, kind)
        )
    """)
    op.execute("""
        CREATE TABLE detector_settings (
            organization_id        INTEGER       PRIMARY KEY REFERENCES organizations(id) ON DELETE CASCADE,
            always_wrong_runs      INTEGER,
            always_wrong_threshold NUMERIC(4, 2),
            updated_at             TIMESTAMPTZ   NOT NULL DEFAULT now(),
            updated_by             INTEGER       REFERENCES users(id),
            CONSTRAINT ck_detector_settings_runs CHECK (always_wrong_runs IS NULL OR always_wrong_runs BETWEEN 2 AND 20),
            CONSTRAINT ck_detector_settings_threshold CHECK (always_wrong_threshold IS NULL
                OR (always_wrong_threshold >= 0 AND always_wrong_threshold <= 10))
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS detector_settings")
    op.execute("DROP TABLE IF EXISTS notification_preferences")
    op.execute("DROP TABLE IF EXISTS notifications")
