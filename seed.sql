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
-- 0) Organisation seed (ADR-077) — les tests/runs préexistants y sont
--    rattachés par migrations.sql (backfill). Le catalogue de modèles et
--    les prompts d'évaluation restent globaux (non rattachés à une org).
-- ---------------------------------------------------------------------
INSERT INTO organizations (id, name, slug)
VALUES (1, 'Bertrand', 'bertrand')
ON CONFLICT (id) DO NOTHING;

-- ---------------------------------------------------------------------
-- 1) Catalogue des modèles
--    model_name = provider reconnu par le dispatch (run.py / evaluate.py) :
--        OpenAI  -> "openai" | "chatgpt" | "gpt"
--        Mistral -> "mistral" | "mistralai"
--        Gemini  -> "gemini" | "google"
--    model_version = identifiant du modèle côté API.
--    (ids alignés sur les commentaires de main.py : 2..5)
-- ---------------------------------------------------------------------
INSERT INTO models (model_id, model_name, model_version) VALUES
    (1, 'chatGPT', 'gpt-4.1-mini'),          -- juge économique (optionnel)
    (2, 'chatGPT', 'gpt-5.2'),               -- modèle testé
    (3, 'mistral', 'mistral-large-latest'),  -- modèle testé
    (4, 'gemini',  'gemini-pro-latest'),     -- modèle testé
    (5, 'gemini',  'gemini-2.5-pro'),        -- juge par défaut (cf. main.py)
    (6, 'albert',  'openweight-large'),      -- juge (API souveraine Etalab, sans web)
    (7, 'albert',  'openweight-medium'),     -- juge (API souveraine Etalab, sans web)
    -- OpenRouter = provider plateforme par défaut (ADR-080, EPIC-001 Phase 1).
    -- model_version = id du modèle au catalogue OpenRouter.
    (8, 'openrouter', 'mistralai/mistral-small-3.2-24b-instruct')  -- juge économique via OpenRouter
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
-- 4) Tests (questions) — exemple
--    - expected_answer NON NULL  => le test est "prêt" (ready) et sera évalué.
--    - Plusieurs variantes acceptées peuvent être séparées par le token ' OU '
--      (evaluate.py garde la meilleure note parmi les variantes).
--    - validity_end_at NULL       => test "actif" (active_only dans load_tests).
-- ---------------------------------------------------------------------
-- Le périmètre « Général » de l'org 1 est créé par migrations.sql (rejouées
-- avant seed.sql par docker-entrypoint.sh). On le résout par sous-requête pour
-- rester robuste à son id auto-incrémenté.
INSERT INTO tests
    (test_id, organization_id, perimeter_id, prompt, expected_answer,
     response_quality_prompt_id, citation_quality_prompt_id,
     validity_start_at, validity_end_at)
VALUES
    (
        1, 1,
        (SELECT id FROM perimeters WHERE organization_id = 1 AND slug = 'general'),
        'Quelle est la capitale de l''Australie ?',
        'Canberra',
        1, 2,
        now(), NULL
    ),
    (
        2, 1,
        (SELECT id FROM perimeters WHERE organization_id = 1 AND slug = 'general'),
        'En quelle année a été signé le traité de Rome instituant la CEE ?',
        '1957 OU en 1957 OU le 25 mars 1957',
        1, 2,
        now(), NULL
    )
ON CONFLICT (test_id) DO NOTHING;

-- ---------------------------------------------------------------------
-- 5) Resynchronisation des séquences d'auto-incrément
--    (nécessaire car on a inséré des ids explicites)
-- ---------------------------------------------------------------------
SELECT setval(pg_get_serial_sequence('organizations',     'id'),             (SELECT COALESCE(MAX(id),             1) FROM organizations));
SELECT setval(pg_get_serial_sequence('models',             'model_id'),       (SELECT COALESCE(MAX(model_id),       1) FROM models));
SELECT setval(pg_get_serial_sequence('prompt_types',       'prompt_type_id'), (SELECT COALESCE(MAX(prompt_type_id), 1) FROM prompt_types));
SELECT setval(pg_get_serial_sequence('evaluation_prompts', 'prompt_id'),      (SELECT COALESCE(MAX(prompt_id),      1) FROM evaluation_prompts));
SELECT setval(pg_get_serial_sequence('tests',              'test_id'),        (SELECT COALESCE(MAX(test_id),        1) FROM tests));
