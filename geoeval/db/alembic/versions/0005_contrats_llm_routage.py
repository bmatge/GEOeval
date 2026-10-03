"""Contrats LLM et politique de routage (ADR-089 §2.6, chantier E5).

Révision : 0005
Précédente : 0004

Arbitrages : un contrat couvre une famille, restreignable à des modèles ; la
politique « souverain / UE » vise les notateurs ; un contrat expiré ou épuisé
bloque (pas de repli silencieux sur la clé plateforme) ; les clés BYOK
(org_credentials) sont converties en contrats, la table reste intacte pour un
retour arrière et sera supprimée par une révision ultérieure.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Famille du fournisseur à partir de models.model_name (miroir de llm_clients._FAMILY_BY_NAME).
_FAMILY_SQL = """
    CASE lower(m.model_name)
        WHEN 'openai' THEN 'openai' WHEN 'chatgpt' THEN 'openai' WHEN 'gpt' THEN 'openai'
        WHEN 'albert' THEN 'albert' WHEN 'etalab' THEN 'albert'
        WHEN 'openai-compatible' THEN 'generic' WHEN 'compatible-openai' THEN 'generic'
        WHEN 'mistral' THEN 'mistral' WHEN 'mistralai' THEN 'mistral'
        WHEN 'gemini' THEN 'gemini' WHEN 'google' THEN 'gemini'
        WHEN 'openrouter' THEN 'openrouter'
    END
"""


def upgrade() -> None:
    op.execute("ALTER TABLE models ADD COLUMN hosting TEXT")
    # Albert (infrastructure publique française) : hébergé dans l'UE.
    op.execute("UPDATE models SET hosting = 'eu' WHERE lower(model_name) IN ('albert', 'etalab')")
    op.execute("""
        CREATE TABLE llm_contracts (
            id                SERIAL PRIMARY KEY,
            organization_id   INTEGER       NOT NULL REFERENCES organizations(id),
            family            TEXT          NOT NULL,
            label             TEXT          NOT NULL,
            reference         TEXT,
            base_url          TEXT,
            api_key_encrypted TEXT,
            extra_headers     JSONB,
            model_ids         JSONB         NOT NULL DEFAULT '[]'::jsonb,
            valid_from        DATE,
            valid_to          DATE,
            cap_eur           NUMERIC(12, 2),
            hosting           TEXT,
            sovereign         BOOLEAN       NOT NULL DEFAULT false,
            is_active         BOOLEAN       NOT NULL DEFAULT true,
            created_at        TIMESTAMPTZ   NOT NULL DEFAULT now(),
            created_by        INTEGER       REFERENCES users(id),
            CONSTRAINT ck_llm_contracts_hosting CHECK (hosting IS NULL OR hosting IN ('eu', 'non_eu')),
            CONSTRAINT ck_llm_contracts_dates CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from)
        )
    """)
    op.execute("CREATE INDEX ix_llm_contracts_org_family ON llm_contracts (organization_id, family)")
    op.execute("""
        CREATE TABLE routing_policies (
            organization_id  INTEGER     PRIMARY KEY REFERENCES organizations(id),
            allowed_families JSONB,
            sovereign_only   BOOLEAN     NOT NULL DEFAULT false,
            eu_only          BOOLEAN     NOT NULL DEFAULT false,
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_by       INTEGER     REFERENCES users(id)
        )
    """)
    op.execute("ALTER TABLE usage ADD COLUMN contract_id INTEGER REFERENCES llm_contracts(id)")
    op.execute("CREATE INDEX ix_usage_contract_id ON usage (contract_id)")
    # Clés BYOK → contrats restreints au modèle d'origine (même blob chiffré).
    op.execute(f"""
        INSERT INTO llm_contracts (organization_id, family, label, base_url, api_key_encrypted,
                                   extra_headers, model_ids, is_active, created_at)
        SELECT c.organization_id, {_FAMILY_SQL}, 'Clé BYOK — ' || m.model_version, c.base_url,
               c.api_key_encrypted, c.extra_headers, jsonb_build_array(c.model_id), c.is_active, c.created_at
        FROM org_credentials c
        JOIN models m ON m.model_id = c.model_id
        WHERE {_FAMILY_SQL} IS NOT NULL
        ORDER BY c.id
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_usage_contract_id")
    op.execute("ALTER TABLE usage DROP COLUMN IF EXISTS contract_id")
    op.execute("DROP TABLE IF EXISTS routing_policies")
    op.execute("DROP TABLE IF EXISTS llm_contracts")
    op.execute("ALTER TABLE models DROP COLUMN IF EXISTS hosting")
