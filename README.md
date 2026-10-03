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

Organisation du dépôt (ADR-088 §2.5) : package `geoeval/` (`core/`, `db/`, `web/`, `worker/`),
`scripts/`, `deploy/`, `docs/`, `tests/`. Seuls `Dockerfile` et `docker-compose.yml` restent à la racine
(contrat spawn).

```
scripts/legacy/main.py, mainUnitaire.py   ← CLI historiques (orchestration hors UI)
        │
        ├── geoeval/core/load.py         ← charge les tests actifs depuis la base
        ├── geoeval/core/run.py          ← PHASE RUN : appelle les modèles testés, stocke les réponses
        │       └── geoeval/core/llm_clients.py   ← clients API + singletons + retry/backoff
        └── geoeval/core/evaluate.py     ← PHASE ÉVALUATION : appelle les LLM-juges, stocke les notes
                └── geoeval/core/llm_clients.py

geoeval/db/session.py   ← moteur SQLAlchemy + SessionLocal (connexion PostgreSQL via DATABASE_URL)
geoeval/db/models.py    ← modèles ORM SQLAlchemy (schéma de la base)
```

### Deux phases

| Phase           | Fichier       | Rôle                                                                                  |
| --------------- | ------------- | ------------------------------------------------------------------------------------- |
| **RUN**         | `geoeval/core/run.py`      | Pour chaque test, appelle le modèle testé (avec web search) et écrit `runs` + `run_results`. |
| **ÉVALUATION**  | `geoeval/core/evaluate.py` | Pour chaque résultat, appelle le(s) juge(s) et écrit `run_evaluations`.               |

---

## Modèle de données (PostgreSQL)

Défini dans `geoeval/db/models.py` via SQLAlchemy ORM.

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

### Phase RUN (`geoeval/core/run.py`)

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

### Phase ÉVALUATION (`geoeval/core/evaluate.py`)

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

## Fiabilité des appels (`geoeval/core/llm_clients.py`)

- **Singletons de clients** OpenAI / Mistral / Gemini (un par process).
- **Singleton d'agent Mistral** par `model_version`.
- `call_with_retry()` : **backoff exponentiel + jitter** (70–130 %), plafonné, + petit délai fixe
  après succès (throttle soft). Réessais : 8 (OpenAI/Mistral), 10 (Gemini).
- Les presets `*_RETRY_EXCEPTIONS` valent tous `(Exception,)` → **toute** exception est réessayée.

---

## Points d'entrée

### `scripts/legacy/main.py` — run + évaluation en boucle

Exécute, pour une liste de modèles testés (`tested_models_id = [2, 3, 4]` en dur), la phase RUN
puis la phase ÉVALUATION (juge `model_id=5`, 1 passage). Journalisation via `RotatingFileHandler`
(`geoeval.log`, 5 Mo × 3).

```
# mapping (en commentaire dans main.py)
# 2 = gpt-5.2 / 3 = mistral-large-latest / 4 = gemini-pro-latest / 5 = gemini-2.5-pro
```

### `scripts/legacy/mainUnitaire.py` — test unitaire manuel

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
docker compose -f deploy/docker-compose.local.yml up -d   # PostgreSQL sur localhost:5432 (user/pass/db = geoeval)

# 4. Schéma + données de démarrage
python -m scripts.init_db    # crée le schéma (create_all)
psql "postgresql://geoeval:geoeval@localhost:5432/geoeval" -f geoeval/db/seed.sql
#   (ou, sans psql local :)
#   docker compose -f deploy/docker-compose.local.yml exec -T db psql -U geoeval -d geoeval < geoeval/db/seed.sql

# 5. Exécuter
python -m scripts.legacy.main          # run complet + évaluation
python -m scripts.legacy.mainUnitaire  # smoke test OpenAI web search (sans base)
```

> Le `geoeval/db/seed.sql` fournit les modèles connus, deux prompts d'évaluation et deux tests d'exemple.
> Remplace/complète la table `tests` avec tes propres questions pour un vrai benchmark.

### Fichiers du kit de démarrage

| Fichier              | Rôle                                                            |
| -------------------- | -------------------------------------------------------------- |
| `requirements.txt`   | Dépendances Python.                                            |
| `.env.example`       | Modèle de configuration (à copier en `.env`).                 |
| `scripts/init_db.py` | Crée le schéma (`--drop` pour tout recréer).                  |
| `geoeval/db/migrations.sql` | Migrations idempotentes (colonnes ajoutées aux tables existantes). |
| `geoeval/db/seed.sql` | Données de démarrage (models, prompts d'éval, tests d'exemple).|
| `deploy/docker-compose.local.yml` | PostgreSQL local prêt à l'emploi (dev).                 |
| `docker-compose.yml` | Déploiement complet (web + db) au contrat VibeLab/spawn.      |
| `Dockerfile` + `deploy/docker-entrypoint.sh` | Image de l'UI web : attente DB, schéma, seed, uvicorn `:3000`. |

---

## Hiérarchie des entités (ADR-089, chantier E1)

Les organisations forment un arbre ministère › direction › service (`parent_id` + chemin
matérialisé). Un admin plateforme crée, qualifie et rattache les entités dans
`/admin/organizations` ou par l'API (`POST /api/v1/orgs`, `PATCH /api/v1/orgs/{slug}`). La liste
blanche de modèles d'une entité borne toutes ses sous-entités (intersection sur la chaîne).

Les rôles s'héritent vers le bas (E2) : un rôle posé sur un ministère vaut sur toutes ses directions et
services, jamais au-dessus. Il en va de même pour les jetons d'API. Un org_admin crée, qualifie et déplace
les sous-entités de son périmètre depuis *Paramètres › sous-entités* ; créer une racine ou sortir une
entité de son périmètre reste réservé à l'administration plateforme.

## Budget consolidé et alertes (ADR-089, chantier E3)

Le plafond d'une entité couvre sa dépense et celle de ses sous-entités. Un lancement est refusé si un seul
plafond de la chaîne serait dépassé, et une exécution programmée qui le dépasserait est sautée et tracée sur
la planification. Les seuils de 80 % et 100 % produisent une alerte par période : bandeau dans l'application,
email aux administrateurs de l'entité qui porte le plafond si un SMTP est configuré (`GEOEVAL_SMTP_*`, voir
`.env.example`), jauges `geoeval_budget_*` sur `/metrics`.

## Pools de questions, thèmes et domaines officiels (ADR-089, chantier E4)

Un **pool** regroupe des questions d'une entité pour les partager *par référence* : privé, avec ses
sous-entités, ou avec toutes les entités (*Configurer › Pools de questions*). Une entité l'exécute en
l'**abonnant à un de ses périmètres** : ses questions rejoignent alors chaque évaluation du périmètre, sans
copie (une correction de la réponse attendue profite à tous les abonnés). Un pool peut en inclure d'autres
(sans cycle), mais une inclusion n'élargit jamais le partage : seuls les pools visibles de l'entité qui
exécute fournissent des questions.

Les **thèmes** sont un catalogue commun (*Administrer › Thèmes*, admin plateforme) ; les éditeurs en
étiquettent questions et périmètres, et filtrent la liste des questions par thème. Les **domaines officiels**
d'un périmètre (ex. `service-public.fr`, sous-domaines inclus) donnent, dans le détail d'un run, la part des
citations qui pointent vers une source officielle.

## Contrats LLM et politique de routage (ADR-089, chantier E5)

Une entité peut poser ses propres **contrats** auprès des fournisseurs (*Paramètres › Contrats LLM*, réservé aux
org_admin) : un marché Mistral, Albert ou OpenRouter, avec sa clé, une période de validité et un plafond. Un
contrat vaut pour toute la famille de modèles, ou seulement certains, et ses sous-entités en héritent. Un contrat
expiré ou au plafond bloque les appels : rien ne part en silence sur la clé de la plateforme. Pour y revenir, on
désactive le contrat. La consommation est imputée au contrat (`usage.contract_id`).

La **politique de routage** de l'entité restreint les fournisseurs autorisés et peut imposer des notateurs
souverains ou hébergés dans l'UE. Une sous-entité ne peut que la durcir. Elle est vérifiée au lancement comme à
chaque échéance programmée, et les formulaires ne proposent que les modèles conformes. Les anciennes clés BYOK ont
été converties en contrats.

## Cycle de vie des questions et campagnes (ADR-089, chantier E8)

Une question naît **publiée** ou en **brouillon** ; un brouillon n'entre dans aucune évaluation, aucun pool et
aucune campagne tant qu'il n'est pas publié. Une question **retirée** sort des évaluations sans que son historique
soit touché ; elle peut être republiée.

Une **campagne** (*Configurer › Campagnes*, administrateurs de l'entité) fait exécuter un protocole commun par des
entités désignées de son sous-arbre : les questions publiées d'un pool, les mêmes IA évaluées, les mêmes notateurs,
à la même fréquence. À l'activation, ce protocole est figé ; chaque participant l'exécute ensuite à ses frais et
sous ses contrats (un participant bloqué par son budget, un contrat ou sa politique de routage est sauté et tracé).
La page de la campagne compare les notes par participant et par IA, avec l'écart d'une exécution à l'autre.

## Notifications (ADR-089, chantier E7)

Le lien *Notifications* de l'en-tête ouvre une boîte de réception personnelle, toutes entités confondues. Les
notifications partent selon le rôle dans l'entité : budget à 80 % ou atteint, et contrat LLM expirant (J-30, J-7)
ou expiré pour les administrateurs ; évaluation en échec et « question toujours fausse » pour les éditeurs et
administrateurs. Chacun choisit les types qu'il reçoit aussi par email (*Préférences d'email*). Le détecteur
« question toujours fausse » alerte quand une question reste sous un seuil sur plusieurs évaluations d'affilée
pour une même IA évaluée (défaut : 3 évaluations sous 5/10, réglable par entité dans *Paramètres*, et hérité
par les sous-entités) ; une seule alerte par série.

## SSO OIDC et préparatifs ProConnect (ADR-089, chantier E6)

Le SSO est optionnel et se configure par variables d'environnement (`.env.example`). Un compte est retrouvé par
son identité chez le fournisseur (`issuer` + `sub`) et non par son email : quand l'email change (mutation), le
compte suit et l'adresse est mise à jour si elle est libre. Un compte existant n'est rattaché par email que si le
fournisseur atteste l'adresse, et jamais s'il est déjà lié à une autre identité chez ce fournisseur.
`OIDC_PROFILE=proconnect` pose les défauts de ProConnect (libellé, scopes, `usual_name`, `siret`, email de
confiance) ; le branchement réel (rattachement par SIRET, `userinfo` en JWT) reste à faire.

## Ressources front vendorisées (ADR-088 lot 1.6)

DSFR 1.13.0 (CSS, JS, fontes Marianne, icônes), Chart.js 4.4.1, dsfr-chart 2.1.1 et dsfr-data 0.42.0 sont
servis depuis `/static/vendor/<paquet>-<version>/…` : aucun CDN à l'exécution (réseau fermé, CSP stricte).
`geoeval/web/static/vendor/MANIFEST.json` porte les versions, licences et empreintes SHA-256, vérifiées en CI.

```bash
python -m scripts.vendor_assets --verify                      # conformité au manifeste
python -m scripts.vendor_assets --refresh [--registry <Nexus>] # monter de version (éditer PACKAGES, puis les gabarits)
```

Licence : MIT pour l'ensemble, sauf la fonte Marianne dont l'usage est réservé à l'État (CGU du DSFR).

## Migrations de schéma (ADR-088 lot 1.4)

Alembic, piloté par `python -m scripts.migrate` (attente de la base → `alembic upgrade head` → seed
idempotente). Dans le compose, le service `migrate` s'exécute **avant** web et worker. La révision
`0001` est convergente : base vierge → instantané `geoeval/db/alembic/schema_base.sql` ; base existante →
rejeu de `geoeval/db/migrations.sql` (gelé). Aucun `alembic stamp` manuel.

```bash
python -m scripts.migrate --check                                   # révision courante vs head + dérive ORM/base
alembic -c geoeval/db/alembic.ini revision --autogenerate -m "ma_modif"   # nouvelle révision depuis models.py
```

## Exploitation (ADR-088 lot 1.5)

- **Logs** : une ligne JSON par événement sur stdout (`GEOEVAL_LOG_FORMAT=json`, défaut hors terminal),
  avec `service`, `request_id` (repris de `X-Request-ID` ou généré, renvoyé dans la réponse), `job_id`,
  `org_id`. Journal d'accès par gabarit de route avec durée.
- **Sondes** : web `/healthz` (processus), `/readyz` (base + schéma, 503 sinon) ; worker sur le port
  interne `GEOEVAL_WORKER_PORT` (9100) : `/healthz` (503 si aucun signe de vie depuis 90 s), `/readyz`.
- **Métriques Prometheus** : `/metrics` (web et worker) — requêtes HTTP par route et statut, durées,
  jobs exécutés et durée, file de jobs par statut, appels LLM par famille et issue.

## API v1 (ADR-088 §2.3 — API first)

Sous-application montée sur `/api/v1`, documentation interactive sur `/api/v1/docs`.
Ressources par organisation : `/api/v1/orgs/{slug}/…` (périmètres, questions et vérité de référence,
modèles, évaluations, statistiques, planifications, jobs, jetons), en lecture et en écriture pour le corpus
(editor+). Jamais de suppression de question : désactivation (ADR-076). Les évaluations et statistiques sont lisibles sans
authentification ; le reste demande un **jeton d'organisation** (`Authorization: Bearer geoeval_…`,
créé par un org_admin dans *Paramètres*) ou une session navigateur. Erreurs au format
`application/problem+json` (RFC 9457).

```bash
curl -s -H "Authorization: Bearer $GEOEVAL_TOKEN" https://geoeval.lab.miweb.run/api/v1/me
curl -s -X POST -H "Authorization: Bearer $GEOEVAL_TOKEN" -H "Content-Type: application/json" \
  -d '{"perimeter_id": 1, "tested_models": ["openai/gpt-5.2"], "judge_models": ["openweight-large"], "test_ids": [1, 2]}' \
  https://geoeval.lab.miweb.run/api/v1/orgs/mon-org/runs        # 202 + job ; suivi : /api/v1/orgs/mon-org/jobs/{id}
```

## Interface web (UI DSFR)

Une UI complète est fournie (FastAPI + Jinja2 + **Système de Design de l'État** / DSFR).
Elle réutilise directement le cœur Python (`geoeval/db`, `geoeval/core`).

```bash
python -m scripts.run_web            # http://127.0.0.1:8000
python -m scripts.run_web --reload   # rechargement auto (dev)
# ou : uvicorn geoeval.web.app:app --reload
```

Pages disponibles :

| Page | Rôle |
| --- | --- |
| **Accueil** (`/`) | Présentation : hero, « comment ça marche » (3 étapes), guide d'utilisation, compteurs. |
| **Tableau de bord** (`/dashboard`) | Classement des modèles par score moyen (réponse / citation) + derniers runs. |
| **Runs** (`/runs`, `/runs/{id}`) | Liste des runs et détail : question → réponse → sources → notes des juges. |
| **Tests** (`/tests`) | Création / édition / (dé)activation des tests (questions + réponses attendues). |
| **Prompts d'évaluation** (`/prompts`) | Gestion des rubriques utilisées par les juges. |
| **Modèles** (`/models`) | Catalogue : ajout/édition/désactivation, URL de base, clé API (stockée masquée, repli sur l'env), en-têtes HTTP JSON. Provider « compatible-openai » pour brancher tout endpoint /chat/completions comme juge. |
| **Lancer un run** (`/launch`) | Choix des modèles testés + juges + répétitions + tests à inclure → exécution en tâche de fond. |
| **Planification** (`/schedules`) | Runs programmés : one-shot à date/heure, quotidien, hebdomadaire ou toutes les N heures (heure de Paris). Stockés en base (survivent aux redéploiements), scheduler intégré (poll 30 s). |
| **Jobs** (`/jobs`, `/jobs/{id}`) | Suivi live d'un run (barre de progression + journal en temps réel). |

Détails d'implémentation :

- **`geoeval/web/app.py`** — routes FastAPI. **`geoeval/web/services.py`** — requêtes/agrégations.
  **`geoeval/worker/`** — file de jobs persistée (`jobs`, `job_logs`) et processus worker
  `python -m geoeval.worker.main` (ADR-088 lot 1.2 ; `GEOEVAL_INLINE_WORKER=1` pour un thread dans le web en dev).
  Historique : exécution des runs en tâche de fond (un worker unique sérialise les
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

- ✅ **Corrigé — juge OpenAI** (`geoeval/core/evaluate.py`, branche `openai`) : l'affectation chaînée
  involontaire `respo=nse = ...` provoquait un `NameError` (variable `response` inexistante) dès
  qu'un juge OpenAI était utilisé. Remplacée par `response = ...`.
- ✅ **Corrigé — `todo.md`** : `evaluate_run` accepte désormais des juges par **nom de modèle**
  (`{"model": "gpt-5.2", "repeats": 2}`) ou par id (`{"model_id": 2, "repeats": 2}`), avec
  résolution nom → `model_id` en interne (`resolve_model`). `execute_run` accepte aussi
  un nom **ou** un id pour le modèle testé. `scripts/legacy/main.py` utilise maintenant les noms de modèles.
- **Extraction de citations naïve** : simple regex sur les URLs du texte, indépendante des
  métadonnées de sources renvoyées par les API (OpenAI renvoie pourtant `web_search_call.action.sources`).
- ✅ **Corrigé — retry trop large** : `call_with_retry` remonte désormais immédiatement
  (`LLMCallError`, sans retry) les erreurs non transitoires : HTTP 400/401/403/404/422 et les
  429 « quota dur » (plan/facturation, `limit: 0`). Les vrais rate limits par minute et les
  erreurs réseau restent réessayés avec backoff.
- **Imports morts** dans `geoeval/core/evaluate.py` (`Model`, `Tuple`, `List`) et un `__import__("google.genai")`
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
