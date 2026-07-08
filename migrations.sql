-- Migrations idempotentes (rejouées à chaque démarrage du conteneur, après
-- init_db.py — create_all crée les tables manquantes mais n'ALTÈRE jamais
-- les tables existantes, d'où ce fichier pour les colonnes ajoutées).

-- ---------------------------------------------------------------------
-- ADR-076 : config API en base + flag "juge" par modèle
-- ---------------------------------------------------------------------
ALTER TABLE models ADD COLUMN IF NOT EXISTS base_url      TEXT;
ALTER TABLE models ADD COLUMN IF NOT EXISTS api_key       TEXT;
ALTER TABLE models ADD COLUMN IF NOT EXISTS extra_headers JSONB;
ALTER TABLE models ADD COLUMN IF NOT EXISTS is_active     BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE models ADD COLUMN IF NOT EXISTS is_judge      BOOLEAN NOT NULL DEFAULT TRUE;

-- ---------------------------------------------------------------------
-- ADR-077 : tenancy multi-org. Colonnes organization_id + backfill vers
-- l'org seed « Bertrand » (id=1) pour préserver l'historique.
-- ---------------------------------------------------------------------
ALTER TABLE tests           ADD COLUMN IF NOT EXISTS organization_id BIGINT;
ALTER TABLE runs            ADD COLUMN IF NOT EXISTS organization_id BIGINT;
ALTER TABLE scheduled_runs  ADD COLUMN IF NOT EXISTS organization_id BIGINT;

-- Org seed (indispensable pour le backfill qui suit).
INSERT INTO organizations (id, name, slug)
VALUES (1, 'Bertrand', 'bertrand')
ON CONFLICT (id) DO NOTHING;

-- Backfill : toutes les entités préexistantes vont dans l'org seed.
UPDATE tests          SET organization_id = 1 WHERE organization_id IS NULL;
UPDATE runs           SET organization_id = 1 WHERE organization_id IS NULL;
UPDATE scheduled_runs SET organization_id = 1 WHERE organization_id IS NULL;

-- FK vers organizations (idempotence via pg_constraint).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tests_organization_id_fkey') THEN
        ALTER TABLE tests
            ADD CONSTRAINT tests_organization_id_fkey
            FOREIGN KEY (organization_id) REFERENCES organizations(id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'runs_organization_id_fkey') THEN
        ALTER TABLE runs
            ADD CONSTRAINT runs_organization_id_fkey
            FOREIGN KEY (organization_id) REFERENCES organizations(id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'scheduled_runs_organization_id_fkey') THEN
        ALTER TABLE scheduled_runs
            ADD CONSTRAINT scheduled_runs_organization_id_fkey
            FOREIGN KEY (organization_id) REFERENCES organizations(id);
    END IF;
END $$;

-- NOT NULL après backfill (idempotent en Postgres).
ALTER TABLE tests           ALTER COLUMN organization_id SET NOT NULL;
ALTER TABLE runs            ALTER COLUMN organization_id SET NOT NULL;
ALTER TABLE scheduled_runs  ALTER COLUMN organization_id SET NOT NULL;

-- Index utiles pour les filtres par org.
CREATE INDEX IF NOT EXISTS ix_tests_organization_id          ON tests(organization_id);
CREATE INDEX IF NOT EXISTS ix_runs_organization_id           ON runs(organization_id);
CREATE INDEX IF NOT EXISTS ix_scheduled_runs_organization_id ON scheduled_runs(organization_id);

-- Sync des séquences (l'INSERT explicite de l'id=1 ne les fait pas avancer).
SELECT setval(pg_get_serial_sequence('organizations', 'id'),
              GREATEST((SELECT COALESCE(MAX(id), 1) FROM organizations), 1));

-- ---------------------------------------------------------------------
-- ADR-077 §5–6 (PR#13) : index pratiques pour la vue audit et lookup
-- des invitations par token / par org.
-- ---------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS ix_invitations_org_id  ON invitations(org_id);
CREATE INDEX IF NOT EXISTS ix_invitations_email   ON invitations(email);
CREATE INDEX IF NOT EXISTS ix_audit_log_org_at    ON audit_log(org_id, at DESC);
CREATE INDEX IF NOT EXISTS ix_audit_log_user_at   ON audit_log(user_id, at DESC);

-- ---------------------------------------------------------------------
-- ADR-078 §1–2 (PR#14) : BYOK par org. Une seule clé active (org, modèle).
-- ---------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS uq_org_credentials_org_model
    ON org_credentials(organization_id, model_id);

-- ---------------------------------------------------------------------
-- ADR-078 §3-5 (PR#15) : pricing versionné, usage row-par-appel, budget.
-- ---------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS ix_model_pricing_active
    ON model_pricing(model_id) WHERE effective_to IS NULL;
CREATE INDEX IF NOT EXISTS ix_usage_org_ts
    ON usage(organization_id, ts DESC);
CREATE INDEX IF NOT EXISTS ix_usage_run_id
    ON usage(run_id);

-- Seed de pricing par défaut (indicatif — l'admin plateforme peut versionner).
-- Prix approximatifs en €/M tokens (fixture, à ajuster).
INSERT INTO model_pricing (model_id, input_price_per_1m_tokens, output_price_per_1m_tokens)
SELECT model_id, 5.00, 15.00 FROM models
WHERE NOT EXISTS (
    SELECT 1 FROM model_pricing p WHERE p.model_id = models.model_id
);

-- ---------------------------------------------------------------------
-- ADR-079 (PR#16) : is_sovereign sur models + index partiel sur la version
-- courante de la vérité de référence.
-- ---------------------------------------------------------------------
ALTER TABLE models ADD COLUMN IF NOT EXISTS is_sovereign BOOLEAN NOT NULL DEFAULT FALSE;
UPDATE models SET is_sovereign = TRUE WHERE model_name = 'albert';

CREATE INDEX IF NOT EXISTS ix_test_ground_truth_active
    ON test_ground_truth(test_id) WHERE valid_to IS NULL;

-- Prompt juge « conformité » (schéma labels catégoriels ADR-079 §3) — déplacé
-- dans seed.sql pour ne pas casser un boot fresh (prompt_types seed y vit).

-- ---------------------------------------------------------------------
-- ADR-079 §2, §5-6 (PR#17) : gold set humain + métriques d'accord.
-- ---------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS uq_gold_annotations_test_run_annotator
    ON gold_annotations(test_id, run_id, annotator_email);
CREATE INDEX IF NOT EXISTS ix_gold_annotations_run_id
    ON gold_annotations(run_id);

-- ---------------------------------------------------------------------
-- PR#18 : Périmètre (objet intermédiaire org → tests). Chaque question
-- est rattachée à un périmètre. Backfill vers un périmètre « Général »
-- créé automatiquement par organisation.
-- ---------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS uq_perimeters_org_slug
    ON perimeters(organization_id, slug);
CREATE INDEX IF NOT EXISTS ix_perimeters_org_id ON perimeters(organization_id);

ALTER TABLE tests           ADD COLUMN IF NOT EXISTS perimeter_id BIGINT;
ALTER TABLE scheduled_runs  ADD COLUMN IF NOT EXISTS perimeter_id BIGINT;
ALTER TABLE runs            ADD COLUMN IF NOT EXISTS perimeter_id BIGINT;

INSERT INTO perimeters (organization_id, name, slug, description)
SELECT o.id, 'Général', 'general', 'Périmètre par défaut créé automatiquement.'
FROM organizations o
WHERE NOT EXISTS (
    SELECT 1 FROM perimeters p WHERE p.organization_id = o.id AND p.slug = 'general'
);

UPDATE tests t
SET perimeter_id = p.id
FROM perimeters p
WHERE t.perimeter_id IS NULL
  AND p.organization_id = t.organization_id
  AND p.slug = 'general';

UPDATE scheduled_runs sr
SET perimeter_id = p.id
FROM perimeters p
WHERE sr.perimeter_id IS NULL
  AND p.organization_id = sr.organization_id
  AND p.slug = 'general';

UPDATE runs r
SET perimeter_id = p.id
FROM perimeters p
WHERE r.perimeter_id IS NULL
  AND p.organization_id = r.organization_id
  AND p.slug = 'general';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tests_perimeter_id_fkey') THEN
        ALTER TABLE tests ADD CONSTRAINT tests_perimeter_id_fkey
            FOREIGN KEY (perimeter_id) REFERENCES perimeters(id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'scheduled_runs_perimeter_id_fkey') THEN
        ALTER TABLE scheduled_runs ADD CONSTRAINT scheduled_runs_perimeter_id_fkey
            FOREIGN KEY (perimeter_id) REFERENCES perimeters(id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'runs_perimeter_id_fkey') THEN
        ALTER TABLE runs ADD CONSTRAINT runs_perimeter_id_fkey
            FOREIGN KEY (perimeter_id) REFERENCES perimeters(id);
    END IF;
END $$;

ALTER TABLE tests           ALTER COLUMN perimeter_id SET NOT NULL;
ALTER TABLE scheduled_runs  ALTER COLUMN perimeter_id SET NOT NULL;

CREATE INDEX IF NOT EXISTS ix_tests_perimeter_id           ON tests(perimeter_id);
CREATE INDEX IF NOT EXISTS ix_scheduled_runs_perimeter_id  ON scheduled_runs(perimeter_id);
CREATE INDEX IF NOT EXISTS ix_runs_perimeter_id            ON runs(perimeter_id);

-- ---------------------------------------------------------------------
-- EPIC-001 Phase 1 (ADR-080 §6.3) : coût réel provider en USD
-- (NULL = coût estimé par pricing ; renseigné = coût réel OpenRouter,
--  cost_eur étant alors la conversion à l'ingestion via USD_EUR_RATE)
-- ---------------------------------------------------------------------
ALTER TABLE usage ADD COLUMN IF NOT EXISTS cost_usd NUMERIC(12, 6);

-- ---------------------------------------------------------------------
-- EPIC-001 Phase 2 (ADR-080 §2.2) : web search paramétrable par modèle
-- ---------------------------------------------------------------------
ALTER TABLE models ADD COLUMN IF NOT EXISTS search_config JSONB;

-- Bascule one-shot des modèles testés vers OpenRouter (ADR-080 §6.1) sur les
-- bases EXISTANTES : désactive les lignes directes historiques (2/3/4) tant que
-- les équivalents openrouter n'existent pas encore (ils sont posés par seed.sql,
-- rejoué juste APRÈS ce fichier → la garde devient fausse aux boots suivants,
-- un admin peut donc réactiver une ligne legacy sans qu'elle soit re-désactivée).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM models
        WHERE model_name = 'openrouter'
          AND model_version IN ('openai/gpt-5.2', 'mistralai/mistral-large-2512', 'google/gemini-2.5-pro')
    ) THEN
        UPDATE models SET is_active = FALSE
        WHERE model_name IN ('chatGPT', 'mistral', 'gemini')
          AND model_version IN ('gpt-5.2', 'mistral-large-latest', 'gemini-pro-latest');
    END IF;
END $$;

-- ---------------------------------------------------------------------
-- EPIC-001 Phase 3 (S3.1) : plafond journalier optionnel à côté du
-- mensuel (NULL = illimité). Soft-stop identique (ADR-078 §5).
-- ---------------------------------------------------------------------
ALTER TABLE budgets ADD COLUMN IF NOT EXISTS daily_cap_eur NUMERIC(12, 2);
