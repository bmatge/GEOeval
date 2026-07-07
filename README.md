# GEOeval

Banc d'essai (**benchmark**) d'évaluation de modèles de langage (LLM) avec **recherche web**,
orienté questions factuelles / vérification de faits en français.

L'outil interroge plusieurs modèles « testés » (ChatGPT, Mistral, Gemini) sur une batterie de
questions stockées en base, récupère leurs réponses (avec les sources web citées), puis fait
**noter** ces réponses par un ou plusieurs **LLM-juges** (approche *LLM-as-a-judge*) sur deux
critères :

1. **Qualité de la réponse** (`response_quality`) — la réponse du modèle est comparée à une
   réponse attendue.
2. **Qualité des citations** (`citation_quality`) — pertinence / fiabilité des sources web citées.

Chaque note est un couple `(label, score)` avec `score ∈ [0, 10]`.

---

## Architecture

```
main.py / mainUnitaire.py   ← points d'entrée (orchestration)
        │
        ├── load.py         ← charge les tests actifs depuis la base
        ├── run.py          ← PHASE RUN : appelle les modèles testés, stocke les réponses
        │       └── llm_clients.py   ← clients API + singletons + retry/backoff
        └── evaluate.py     ← PHASE ÉVALUATION : appelle les LLM-juges, stocke les notes
                └── llm_clients.py

db.py        ← moteur SQLAlchemy + SessionLocal (connexion PostgreSQL via DATABASE_URL)
models.py    ← modèles ORM SQLAlchemy (schéma de la base)
```

### Deux phases

| Phase           | Fichier       | Rôle                                                                                  |
| --------------- | ------------- | ------------------------------------------------------------------------------------- |
| **RUN**         | `run.py`      | Pour chaque test, appelle le modèle testé (avec web search) et écrit `runs` + `run_results`. |
| **ÉVALUATION**  | `evaluate.py` | Pour chaque résultat, appelle le(s) juge(s) et écrit `run_evaluations`.               |

---

## Modèle de données (PostgreSQL)

Défini dans `models.py` via SQLAlchemy ORM.

| Table                 | Rôle                                                                                          |
| --------------------- | --------------------------------------------------------------------------------------------- |
| `tests`               | Une question (`prompt`), sa `expected_answer`, et les FK vers les prompts d'évaluation. Versionné par `validity_start_at` / `validity_end_at`. |
| `models`              | Catalogue des modèles. `model_name` = *provider* (ex. `chatGPT`, `mistral`, `gemini`), `model_version` = id API (ex. `gpt-5.2`). |
| `runs`                | Un run = une exécution d'un `tested_model_id` sur l'ensemble des tests. Contient `run_meta` (JSONB).  |
| `run_results`         | Réponse brute (`raw_answer`) + citations extraites (`raw_citations`, JSONB) pour un couple (run, test). |
| `run_evaluations`     | Notes d'un juge : `response_quality_(label,score)` et `citation_quality_(label,score)`. PK = (run, test, judge_model, judge_run_index). |
| `evaluation_prompts`  | Textes des prompts d'évaluation utilisés par les juges (qualité réponse / qualité citation).   |
| `prompt_types`        | Typologie des prompts d'évaluation.                                                             |

### Relations clés

- `tests.response_quality_prompt_id` → `evaluation_prompts.prompt_id`
- `tests.citation_quality_prompt_id` → `evaluation_prompts.prompt_id`
- `runs.tested_model_id` → `models.model_id`
- `run_evaluations.judge_model_id` → `models.model_id`

> ⚠️ Les tables sont supposées **déjà créées et peuplées** en base (tests, models, evaluation_prompts).
> Le code ne fournit ni migration ni script de seed — il fait un `select`, jamais un `create_all`.

---

## Flux détaillé

### Phase RUN (`run.py`)

1. `load_tests()` récupère les tests **actifs** (`validity_end_at IS NULL`) et **prêts**
   (`expected_answer IS NOT NULL`).
2. `execute_run()` appelle `call_tested_llm()` pour chaque test.
3. `call_tested_llm()` aiguille selon `model.model_name` :
   - **OpenAI** → `client.responses.create(...)` avec l'outil `web_search` (localisation FR/Paris).
   - **Mistral** → API *Agents / Conversations* (`beta.agents` + `beta.conversations.start`) avec
     `web_search`. Un **agent est créé une seule fois par `model_version`** (singleton).
   - **Gemini** → `generate_content(...)` avec l'outil `GoogleSearch`.
4. Les URLs de la réponse sont extraites par regex (`extract_urls`) et stockées comme citations.
5. Écriture en base : un `RunRow` + N `RunResult`.

Toutes les réponses des modèles testés partagent un **system prompt** commun
(`build_instructions()`) : assistant généraliste francophone, précision numérique exigée,
date du jour injectée, consigne de répondre plutôt que de demander une clarification.

### Phase ÉVALUATION (`evaluate.py`)

1. Jointure `run_results × tests × evaluation_prompts` (deux alias : prompt réponse + prompt citation).
2. Pour chaque juge (spécifié par `model_id` ou nom de modèle + nombre de répétitions) et chaque répétition :
   - **Qualité réponse** : le juge reçoit le prompt d'éval + `[Réponse attendue]` + `[Réponse du modèle]`.
     La réponse attendue peut contenir plusieurs variantes séparées par le token `' OU '` → on garde la
     meilleure note.
   - **Qualité citation** : le juge reçoit le prompt d'éval + la réponse du modèle.
3. Le juge doit répondre en **JSON strict** (`build_prompt_json_guardrails`) :
   `{"label": "...", "score": 0-10}`. Parsé par `parse_judge_output()` (avec repli : extraction du
   premier bloc `{...}` si le JSON est entouré de texte).
4. `RunEvaluation` est **upserté** (`ON CONFLICT DO UPDATE` sur la PK) → un même juge peut être rejoué
   sans dupliquer les lignes (grâce à `judge_run_index`).

Les juges sont appelés **sans** outil de recherche web et à basse température (0.3).

---

## Fiabilité des appels (`llm_clients.py`)

- **Singletons de clients** OpenAI / Mistral / Gemini (un par process).
- **Singleton d'agent Mistral** par `model_version`.
- `call_with_retry()` : **backoff exponentiel + jitter** (70–130 %), plafonné, + petit délai fixe
  après succès (throttle soft). Réessais : 8 (OpenAI/Mistral), 10 (Gemini).
- Les presets `*_RETRY_EXCEPTIONS` valent tous `(Exception,)` → **toute** exception est réessayée.

---

## Points d'entrée

### `main.py` — run + évaluation en boucle

Exécute, pour une liste de modèles testés (`tested_models_id = [2, 3, 4]` en dur), la phase RUN
puis la phase ÉVALUATION (juge `model_id=5`, 1 passage). Journalisation via `RotatingFileHandler`
(`geoeval.log`, 5 Mo × 3).

```
# mapping (en commentaire dans main.py)
# 2 = gpt-5.2 / 3 = mistral-large-latest / 4 = gemini-pro-latest / 5 = gemini-2.5-pro
```

### `mainUnitaire.py` — test unitaire manuel

Appelle directement `call_gpt52()` sur un prompt d'exemple (vérification d'une affirmation
économique). Utile pour tester la connexion OpenAI + web search sans base ni orchestration.

---

## Configuration

Variables d'environnement (fichier `.env`, chargé via `python-dotenv`) :

| Variable          | Usage                                   |
| ----------------- | --------------------------------------- |
| `DATABASE_URL`    | Chaîne de connexion PostgreSQL (SQLAlchemy). |
| `OPENAI_API_KEY`  | Clé API OpenAI.                          |
| `MISTRAL_API_KEY` | Clé API Mistral.                         |
| `GEMINI_API_KEY`  | Clé API Google Gemini.                   |
| `ALBERT_API_KEY`  | Clé API Albert (Etalab, juge uniquement — sans recherche web). |
| `ALBERT_BASE_URL` | Optionnel, défaut `https://albert.api.etalab.gouv.fr/v1`. |

### Dépendances (implicites — pas de `requirements.txt`)

`sqlalchemy` (+ driver PostgreSQL, ex. `psycopg`/`psycopg2`), `python-dotenv`,
`openai`, `mistralai`, `google-genai`.

---

## Démarrage rapide

```bash
# 1. Dépendances
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configuration
cp .env.example .env         # puis remplir DATABASE_URL + clés API

# 3. Base PostgreSQL (option A : Docker fourni)
docker compose -f docker-compose.local.yml up -d   # PostgreSQL sur localhost:5432 (user/pass/db = geoeval)

# 4. Schéma + données de démarrage
python init_db.py            # crée les 7 tables (create_all)
psql "postgresql://geoeval:geoeval@localhost:5432/geoeval" -f seed.sql
#   (ou, sans psql local :)
#   docker compose -f docker-compose.local.yml exec -T db psql -U geoeval -d geoeval < seed.sql

# 5. Exécuter
python main.py               # run complet + évaluation
python mainUnitaire.py       # smoke test OpenAI web search (sans base)
```

> Le `seed.sql` fournit les modèles connus, deux prompts d'évaluation et deux tests d'exemple.
> Remplace/complète la table `tests` avec tes propres questions pour un vrai benchmark.

### Fichiers du kit de démarrage

| Fichier              | Rôle                                                            |
| -------------------- | -------------------------------------------------------------- |
| `requirements.txt`   | Dépendances Python.                                            |
| `.env.example`       | Modèle de configuration (à copier en `.env`).                 |
| `init_db.py`         | Crée le schéma (`--drop` pour tout recréer).                  |
| `seed.sql`           | Données de démarrage (models, prompts d'éval, tests d'exemple).|
| `docker-compose.local.yml` | PostgreSQL local prêt à l'emploi (dev).                 |
| `docker-compose.yml` | Déploiement complet (web + db) au contrat VibeLab/spawn.      |
| `Dockerfile` + `docker-entrypoint.sh` | Image de l'UI web : attente DB, schéma, seed, uvicorn `:3000`. |

---

## Interface web (UI DSFR)

Une UI complète est fournie (FastAPI + Jinja2 + **Système de Design de l'État** / DSFR).
Elle réutilise directement le cœur Python (`db.py`, `models.py`, `run.py`, `evaluate.py`).

```bash
python run_web.py            # http://127.0.0.1:8000
python run_web.py --reload   # rechargement auto (dev)
# ou : uvicorn webapp.app:app --reload
```

Pages disponibles :

| Page | Rôle |
| --- | --- |
| **Tableau de bord** (`/`) | Classement des modèles par score moyen (réponse / citation) + derniers runs. |
| **Runs** (`/runs`, `/runs/{id}`) | Liste des runs et détail : question → réponse → sources → notes des juges. |
| **Tests** (`/tests`) | Création / édition / (dé)activation des tests (questions + réponses attendues). |
| **Prompts d'évaluation** (`/prompts`) | Gestion des rubriques utilisées par les juges. |
| **Lancer un run** (`/launch`) | Choix des modèles testés + juges + répétitions → exécution en tâche de fond. |
| **Jobs** (`/jobs`, `/jobs/{id}`) | Suivi live d'un run (barre de progression + journal en temps réel). |

Détails d'implémentation :

- **`webapp/app.py`** — routes FastAPI. **`webapp/services.py`** — requêtes/agrégations.
  **`webapp/jobs.py`** — exécution des runs en tâche de fond (un worker unique sérialise les
  runs ; les logs GEOeval sont capturés par job et affichés en direct via polling `/api/jobs/{id}`).
- Les runs longs (N modèles × M tests + juges) tournent **hors requête HTTP** : l'UI reste réactive
  et suit la progression grâce aux callbacks `progress_cb` ajoutés à `execute_run` / `evaluate_run`.
- **DSFR chargé via CDN** (jsDelivr) pour simplifier. Pour un usage hors-ligne / production, vendorer
  les assets DSFR dans un dossier `static/` et servir localement.
- **Imports LLM paresseux** : les SDK `openai` / `mistralai` / `google-genai` ne sont chargés que
  lorsqu'un provider est réellement appelé → l'UI et le CLI démarrent en n'installant que les SDK utiles
  (ex. Mistral + Gemini, sans OpenAI).

---

## Limitations connues / dette technique

Ces points ressortent de la lecture du code (`todo.md` + bugs repérés) :

- ✅ **Corrigé — juge OpenAI** (`evaluate.py`, branche `openai`) : l'affectation chaînée
  involontaire `respo=nse = ...` provoquait un `NameError` (variable `response` inexistante) dès
  qu'un juge OpenAI était utilisé. Remplacée par `response = ...`.
- ✅ **Corrigé — `todo.md`** : `evaluate_run` accepte désormais des juges par **nom de modèle**
  (`{"model": "gpt-5.2", "repeats": 2}`) ou par id (`{"model_id": 2, "repeats": 2}`), avec
  résolution nom → `model_id` en interne (`resolve_model`). `execute_run` accepte aussi
  un nom **ou** un id pour le modèle testé. `main.py` utilise maintenant les noms de modèles.
- **Extraction de citations naïve** : simple regex sur les URLs du texte, indépendante des
  métadonnées de sources renvoyées par les API (OpenAI renvoie pourtant `web_search_call.action.sources`).
- **Retry trop large** : `retry_exceptions = (Exception,)` réessaie même des erreurs non transitoires
  (ex. clé API invalide, erreurs de validation).
- **Imports morts** dans `evaluate.py` (`Model`, `Tuple`, `List`) et un `__import__("google.genai")`
  contourné pour accéder à `types` dans la branche Gemini du juge.

---

## Déploiement (VibeLab / spawn)

Le `docker-compose.yml` racine suit le contrat spawn : service `web` (uvicorn `:3000`
derrière Traefik) + PostgreSQL interne non exposé, schéma et seed appliqués
automatiquement au démarrage (idempotent).

```bash
ssh vps "spawn up geoeval git@github.com:bmatge/GEOeval.git"
```

Les clés API (`OPENAI_API_KEY`, `MISTRAL_API_KEY`, `GEMINI_API_KEY`) ne sont pas
commitées : les poser dans `/opt/apps/geoeval/.env` sur le VPS (survit aux `git pull`
de spawn), puis relancer `spawn up geoeval` pour recharger l'environnement.
