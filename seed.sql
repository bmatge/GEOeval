-- =====================================================================
-- GEOeval — données de démarrage (seed)
--
-- À exécuter APRÈS init_db.py (les tables doivent exister) :
--     psql "$DATABASE_URL" -f seed.sql
-- ou :  psql -h localhost -U geoeval -d geoeval -f seed.sql
--
-- Idempotent : ON CONFLICT DO NOTHING. Les séquences sont resynchronisées
-- à la fin pour que les futurs INSERT auto-incrémentés ne collisionnent pas.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 0) Organisations : AUCUNE seed. Une installation vierge démarre sans
--    org — elles se créent via l'UI (/admin/organizations, admin bootstrappé
--    par GEOEVAL_ADMIN_EMAILS). L'ancienne org seed « bertrand » (ADR-077 §4,
--    utile au backfill v1→v2) ressuscitait à chaque boot après sa
--    suppression volontaire (session 2026-07-20) — retirée.
--    Seuls le catalogue de modèles et les prompts d'évaluation (globaux)
--    sont seedés ici.
-- ---------------------------------------------------------------------

-- ---------------------------------------------------------------------
-- 1) Catalogue des modèles
--    model_name = provider reconnu par le dispatch (run.py / evaluate.py) :
--        OpenAI  -> "openai" | "chatgpt" | "gpt"
--        Mistral -> "mistral" | "mistralai"
--        Gemini  -> "gemini" | "google"
--    model_version = identifiant du modèle côté API.
--    (ids alignés sur les commentaires de main.py : 2..5)
-- ---------------------------------------------------------------------
-- OpenRouter = provider plateforme par défaut pour les modèles testés (ADR-080 §6.1) :
-- les lignes directes historiques (2/3/4) sont conservées mais DÉSACTIVÉES (jamais
-- supprimées — ADR-076, les runs historiques y restent rattachés).
INSERT INTO models (model_id, model_name, model_version, is_active, search_config) VALUES
    (1, 'chatGPT', 'gpt-4.1-mini', TRUE, NULL),           -- juge économique (optionnel)
    (2, 'chatGPT', 'gpt-5.2', FALSE, NULL),               -- testé LEGACY direct → remplacé par #9
    (3, 'mistral', 'mistral-large-latest', FALSE, NULL),  -- testé LEGACY direct → remplacé par #10
    (4, 'gemini',  'gemini-pro-latest', FALSE, NULL),     -- testé LEGACY direct → remplacé par #11
    (5, 'gemini',  'gemini-2.5-pro', TRUE, NULL),         -- juge par défaut (cf. main.py)
    (6, 'albert',  'openweight-large', TRUE, NULL),       -- juge (API souveraine Etalab, sans web)
    (7, 'albert',  'openweight-medium', TRUE, NULL),      -- juge (API souveraine Etalab, sans web)
    -- model_version = id du modèle au catalogue OpenRouter.
    (8, 'openrouter', 'mistralai/mistral-small-3.2-24b-instruct', TRUE, NULL),  -- juge économique
    -- Modèles testés via OpenRouter (ADR-080) : search natif pour OpenAI,
    -- Exa (moteur OpenRouter) pour Mistral/Gemini — mesuré au SPIKE-001.
    (9,  'openrouter', 'openai/gpt-5.2',              TRUE, '{"engine": "native", "max_results": 5}'),
    (10, 'openrouter', 'mistralai/mistral-large-2512', TRUE, '{"engine": "exa", "max_results": 5}'),
    (11, 'openrouter', 'google/gemini-2.5-pro',        TRUE, '{"engine": "exa", "max_results": 5}')
ON CONFLICT (model_id) DO NOTHING;

-- ---------------------------------------------------------------------
-- 2) Types de prompts d'évaluation
-- ---------------------------------------------------------------------
INSERT INTO prompt_types (prompt_type_id, prompt_type_label) VALUES
    (1, 'response_quality'),
    (2, 'citation_quality')
ON CONFLICT (prompt_type_id) DO NOTHING;

-- ---------------------------------------------------------------------
-- 3) Prompts d'évaluation (rubriques du juge)
--    Le schéma JSON de sortie {"label","score"} est ajouté automatiquement
--    par build_prompt_json_guardrails() dans evaluate.py : ne pas le répéter ici.
-- ---------------------------------------------------------------------
INSERT INTO evaluation_prompts (prompt_id, prompt_type_id, prompt_name, prompt_text) VALUES
    (
        1, 1, 'response_quality_v1',
        'Tu évalues la QUALITÉ FACTUELLE d''une réponse de modèle par rapport à une réponse attendue. ' ||
        'Attribue un score de 0 à 10 : 10 = exacte, complète et cohérente avec la réponse attendue ; ' ||
        '0 = fausse ou hors sujet. Pénalise les erreurs numériques, les imprécisions et les omissions ' ||
        'importantes. Le champ "label" résume en une phrase courte la justification de la note.'
    ),
    (
        2, 2, 'citation_quality_v1',
        'Tu évalues la QUALITÉ DES CITATIONS/SOURCES présentes dans la réponse du modèle. ' ||
        'Attribue un score de 0 à 10 : 10 = sources fiables, pertinentes, vérifiables et directement ' ||
        'liées aux affirmations ; 0 = aucune source, sources non pertinentes ou non fiables. ' ||
        'Le champ "label" résume en une phrase courte la justification de la note.'
    ),
    (
        3, 1, 'response_quality_reference',
        'Tu évalues la CONFORMITÉ d''une réponse de modèle à une VÉRITÉ DE RÉFÉRENCE ' ||
        'sourcée. Pas d''opinion : compare uniquement à la référence fournie. ' ||
        'Attribue un label parmi : ' ||
        '"conforme" (réponse alignée avec la référence, complète et sans erreur), ' ||
        '"partiel" (réponse partiellement conforme — omissions ou imprécisions), ' ||
        '"non_conforme" (réponse contradictoire avec la référence), ' ||
        '"hors_sujet" (réponse sans lien avec la question). ' ||
        'Attribue AUSSI un score numérique 0-10 (10=parfaitement conforme, 0=hors sujet).'
    )
ON CONFLICT (prompt_id) DO NOTHING;

-- ---------------------------------------------------------------------
-- 4) Tests (questions) : AUCUNE seed — les questions se créent via l'UI
--    (elles appartiennent à une org, qui n'existe plus en seed).
-- ---------------------------------------------------------------------

-- ---------------------------------------------------------------------
-- 5) Resynchronisation des séquences d'auto-incrément
--    (nécessaire car on a inséré des ids explicites)
-- ---------------------------------------------------------------------
SELECT setval(pg_get_serial_sequence('organizations',     'id'),             (SELECT COALESCE(MAX(id),             1) FROM organizations));
SELECT setval(pg_get_serial_sequence('models',             'model_id'),       (SELECT COALESCE(MAX(model_id),       1) FROM models));
SELECT setval(pg_get_serial_sequence('prompt_types',       'prompt_type_id'), (SELECT COALESCE(MAX(prompt_type_id), 1) FROM prompt_types));
SELECT setval(pg_get_serial_sequence('evaluation_prompts', 'prompt_id'),      (SELECT COALESCE(MAX(prompt_id),      1) FROM evaluation_prompts));
SELECT setval(pg_get_serial_sequence('tests',              'test_id'),        (SELECT COALESCE(MAX(test_id),        1) FROM tests));
