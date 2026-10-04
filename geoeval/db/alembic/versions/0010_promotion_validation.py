"""Promotion d'un lot rejugé et validation métier des questions (ADR-089 §2.9, fin de E8).

Révision : 0010
Précédente : 0009

Arbitrages : promotion par échange réversible (notes d'origine archivées dans un lot
« origin »), réservée aux org_admin, hors runs de campagne ; validation métier optionnelle,
héritée, à quatre yeux, par des validateurs désignés (org_admin d'office) ; modifier
l'énoncé ou la réponse attendue d'une question publiée la renvoie en relecture.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE tests DROP CONSTRAINT IF EXISTS ck_tests_status")
    op.execute("ALTER TABLE tests ADD CONSTRAINT ck_tests_status "
               "CHECK (status IN ('draft', 'in_review', 'published', 'retired'))")
    op.execute("ALTER TABLE tests ADD COLUMN submitted_by INTEGER REFERENCES users(id) ON DELETE SET NULL")
    op.execute("ALTER TABLE tests ADD COLUMN submitted_at TIMESTAMPTZ")
    op.execute("ALTER TABLE tests ADD COLUMN reviewed_by INTEGER REFERENCES users(id) ON DELETE SET NULL")
    op.execute("ALTER TABLE tests ADD COLUMN reviewed_at TIMESTAMPTZ")
    op.execute("ALTER TABLE tests ADD COLUMN review_comment TEXT")
    op.execute("ALTER TABLE organizations ADD COLUMN review_required BOOLEAN")
    op.execute("ALTER TABLE memberships ADD COLUMN is_validator BOOLEAN NOT NULL DEFAULT false")
    op.execute("ALTER TABLE evaluation_batches ADD COLUMN kind TEXT NOT NULL DEFAULT 'rejudge'")
    op.execute("ALTER TABLE evaluation_batches ADD CONSTRAINT ck_evaluation_batches_kind "
               "CHECK (kind IN ('rejudge', 'origin'))")
    op.execute("ALTER TABLE evaluation_batches ADD COLUMN promoted_at TIMESTAMPTZ")
    op.execute("ALTER TABLE evaluation_batches ADD COLUMN promoted_by INTEGER REFERENCES users(id) ON DELETE SET NULL")
    op.execute("ALTER TABLE runs ADD COLUMN reference_batch_id INTEGER REFERENCES evaluation_batches(id)")
    op.execute("ALTER TABLE runs ADD COLUMN origin_batch_id INTEGER REFERENCES evaluation_batches(id)")


def downgrade() -> None:
    op.execute("ALTER TABLE runs DROP COLUMN IF EXISTS origin_batch_id")
    op.execute("ALTER TABLE runs DROP COLUMN IF EXISTS reference_batch_id")
    op.execute("ALTER TABLE evaluation_batches DROP COLUMN IF EXISTS promoted_by")
    op.execute("ALTER TABLE evaluation_batches DROP COLUMN IF EXISTS promoted_at")
    op.execute("ALTER TABLE evaluation_batches DROP CONSTRAINT IF EXISTS ck_evaluation_batches_kind")
    op.execute("ALTER TABLE evaluation_batches DROP COLUMN IF EXISTS kind")
    op.execute("ALTER TABLE memberships DROP COLUMN IF EXISTS is_validator")
    op.execute("ALTER TABLE organizations DROP COLUMN IF EXISTS review_required")
    for col in ("review_comment", "reviewed_at", "reviewed_by", "submitted_at", "submitted_by"):
        op.execute(f"ALTER TABLE tests DROP COLUMN IF EXISTS {col}")
    # Les questions en relecture redeviennent des brouillons (statut inconnu avant 0010).
    op.execute("UPDATE tests SET status = 'draft' WHERE status = 'in_review'")
    op.execute("ALTER TABLE tests DROP CONSTRAINT IF EXISTS ck_tests_status")
    op.execute("ALTER TABLE tests ADD CONSTRAINT ck_tests_status CHECK (status IN ('draft', 'published', 'retired'))")
