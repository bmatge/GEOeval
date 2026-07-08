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
