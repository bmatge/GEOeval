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
    (5, 'gemini',  'gemini-2.5-pro')         -- juge par défaut (cf. main.py)
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
    )
ON CONFLICT (prompt_id) DO NOTHING;

-- ---------------------------------------------------------------------
-- 4) Tests (questions) — exemple
--    - expected_answer NON NULL  => le test est "prêt" (ready) et sera évalué.
--    - Plusieurs variantes acceptées peuvent être séparées par le token ' OU '
--      (evaluate.py garde la meilleure note parmi les variantes).
--    - validity_end_at NULL       => test "actif" (active_only dans load_tests).
-- ---------------------------------------------------------------------
INSERT INTO tests
    (test_id, prompt, expected_answer,
     response_quality_prompt_id, citation_quality_prompt_id,
     validity_start_at, validity_end_at)
VALUES
    (
        1,
        'Quelle est la capitale de l''Australie ?',
        'Canberra',
        1, 2,
        now(), NULL
    ),
    (
        2,
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
SELECT setval(pg_get_serial_sequence('models',             'model_id'),       (SELECT COALESCE(MAX(model_id),       1) FROM models));
SELECT setval(pg_get_serial_sequence('prompt_types',       'prompt_type_id'), (SELECT COALESCE(MAX(prompt_type_id), 1) FROM prompt_types));
SELECT setval(pg_get_serial_sequence('evaluation_prompts', 'prompt_id'),      (SELECT COALESCE(MAX(prompt_id),      1) FROM evaluation_prompts));
SELECT setval(pg_get_serial_sequence('tests',              'test_id'),        (SELECT COALESCE(MAX(test_id),        1) FROM tests));
