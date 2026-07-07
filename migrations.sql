-- Migrations idempotentes (rejouées à chaque démarrage du conteneur, après
-- init_db.py — create_all crée les tables manquantes mais n'ALTÈRE jamais
-- les tables existantes, d'où ce fichier pour les colonnes ajoutées).

ALTER TABLE models ADD COLUMN IF NOT EXISTS base_url      TEXT;
ALTER TABLE models ADD COLUMN IF NOT EXISTS api_key       TEXT;
ALTER TABLE models ADD COLUMN IF NOT EXISTS extra_headers JSONB;
ALTER TABLE models ADD COLUMN IF NOT EXISTS is_active     BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE models ADD COLUMN IF NOT EXISTS is_judge      BOOLEAN NOT NULL DEFAULT TRUE;
