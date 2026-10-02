# CLAUDE.md — GEOeval

## 1. Le projet en 3 lignes

Benchmark **longitudinal** de LLM avec recherche web : des modèles testés (chatGPT, Mistral,
Gemini) répondent à des questions factuelles françaises stockées en base, puis des LLM-juges
(Mistral, Albert `openweight-*`, …) notent réponse et citations (LLM-as-a-judge, scores 0-10).
UI web FastAPI + DSFR, runs en tâche de fond + planification intégrée.

## 2. Stack

- Langage : Python 3.12, type hints
- Framework : FastAPI + Jinja2 (DSFR 1.13), SQLAlchemy 2 (psycopg2), uvicorn
- Infra : PostgreSQL 16, Docker (contrat spawn VibeLab : web `:3000` + db interne),
  déployé sur https://geoeval.lab.miweb.run (`AUTH=link` depuis le 2026-07-30 — gate
  magic-link devant, PUIS auth applicative : comptes locaux + SSO OIDC optionnel,
  ADR-086 amendée)
- Dépendances critiques : `openai` (aussi pour Albert / endpoints compatibles), `mistralai`
  (v2 — import via fallback `mistralai.client`), `google-genai` ; auth : `bcrypt`,
  `authlib`, `itsdangerous`

## 3. Comment lancer

```bash
# Dev local (Postgres seul)
docker compose -f deploy/docker-compose.local.yml up -d
cp .env.example .env                    # DATABASE_URL + clés API
python -m scripts.init_db && psql "$PGURL" -f geoeval/db/seed.sql   # PGURL = URL libpq (sans +psycopg2)
python -m scripts.run_web               # UI sur http://127.0.0.1:8000
python -m geoeval.worker.main           # worker (runs + planificateur) — ou GEOEVAL_INLINE_WORKER=1 dans le web

# Tests + lint (ADR-088) — DATABASE_URL posé = tests d'intégration inclus, sinon sautés
pip install -r requirements-dev.txt
ruff check . && python -m pytest -q

# Test local du conteneur complet (ce que fait le VPS)
docker compose up -d --build            # nécessite le réseau externe `proxy` + APP_NAME/DOMAIN

# Déploiement
ssh vps "spawn up geoeval"              # clés API dans /opt/apps/geoeval/.env (survit aux pulls)
```

## 4. Structure des dossiers

```
.
├── geoeval/               → package applicatif (ADR-088 §2.5), racine unique des imports
│   ├── core/              → run.py / evaluate.py (phases RUN et ÉVALUATION), load.py, llm_clients.py (cascade clés, retry)
│   ├── db/                → session.py, models.py (23 tables), migrations.sql (idempotent), seed.sql
│   ├── web/               → app.py (assemblage), ui/ (routers HTML minces), api/ (API v1 sur /api/v1 : deps jetons/session, problems RFC 9457, schemas, v1/ routers), launching.py (règles : liste blanche, devis, budget), api_tokens.py, services.py (DAO), auth*, tenancy… + templates/
│   └── worker/            → main.py (processus worker, SIGTERM gracieux), jobs.py (file `jobs` + `job_logs`, SKIP LOCKED), scheduler.py (verrou advisory)
├── scripts/               → init_db, set_password, run_web (`python -m scripts.<nom>`) ; legacy/ = CLI historiques
├── deploy/                → docker-entrypoint.sh, docker-compose.local.yml
├── tests/ + pyproject.toml → pytest (unitaires sans base + `integration` sur PostgreSQL), ruff ; CI .github/workflows/ci.yml
├── docs/                  → adr/ (ADR-080, 088, 089), architecture.md, epics/, spikes/
└── Dockerfile + docker-compose.yml     → racine imposée par le contrat spawn ; services web (entrypoint : db → init_db → migrations → seed → uvicorn :3000), worker, db
```

## 5. Conventions

- Style : type hints systématiques ; docstrings courtes en français
- Branches : `master` protégé par convention, **tout passe par PR** (merge par Bertrand)
- Commits : Conventional Commits (`feat:`, `fix:`, `docs:`…), messages en français
- Migrations : jamais d'ALTER manuel — ajouter à `geoeval/db/migrations.sql` (idempotent,
  `ADD COLUMN IF NOT EXISTS`), rejoué à chaque démarrage du conteneur

## 6. Ce que Claude doit toujours faire

- **`ruff check . && python -m pytest -q` verts avant chaque PR** (la CI GitHub Actions les rejoue avec PostgreSQL + `docker build`)
- **Tester sur le stack Docker local avant chaque PR** (build + up + curl des pages touchées)
- Vérifier un déploiement par `curl -fsI https://geoeval.lab.miweb.run` (302 = gate, normal)
  et, pour le contenu, depuis le conteneur (`docker exec geoeval-web-1 …` via `ssh vps`)
- Préserver l'**historique des runs** : jamais de suppression de modèles/runs référencés
  (cf. ADR-076 dans le vault) — désactivation uniquement
- Documenter toute décision structurante dans `~/Documents/Obsidian/30-Knowledge/ADR/`
- Loguer la session dans `~/Documents/Obsidian/20-Sessions/` (ajouter `GEOeval` à `projects:`)

## 7. Ce que Claude ne doit jamais faire

- Commit direct sur `master`
- `git push --force` sans demander
- Installer une lib non listée dans `requirements.txt` sans en parler
- Toucher aux secrets : `.env` local, `/opt/apps/geoeval/.env` sur le VPS (diagnostic par
  noms de variables uniquement, jamais afficher les valeurs)
- Relancer `spawn up geoeval --auth …` avec un autre mode : `AUTH=link` est voulu depuis
  le 2026-07-30 (gate magic-link + auth applicative derrière — double login assumé).
  ⚠️ Le SSO applicatif dépend de l'application Authentik `geoeval-sso` (provider
  `geoeval-oidc`) : le slug `geoeval` appartient à `app-auth.py` (bypass gate ADR-062)
- Poser `DEV_FAKE_EMAIL` en prod (bypass complet de l'auth applicative)

## 8. Références externes

- Note projet dans le vault : `~/Documents/Obsidian/10-Projects/GEOeval.md`
- ADR-076 (historique inviolable, config modèles, planification) : vault `30-Knowledge/ADR/`
- ADR-088 (stack conservée, API first, refacto en 2 lots vers Nubo) · ADR-089 (hiérarchie d'entités,
  budgets consolidés, pools, contrats LLM, ProConnect) : `docs/adr/` · schémas : `docs/architecture.md`
- Backlog : issues GitHub **désactivées** sur ce repo → suivre via PR + `todo.md`
- Proto : https://geoeval.lab.miweb.run · API v1 : `/api/v1/docs` (jeton `Authorization: Bearer`, créé dans Paramètres de l'org) · plateforme : ADR-038 (spawn), ADR-056 (secrets partagés)

---

⚠️ **Garder ce fichier sous 200 lignes.** Si ça dépasse, déplacer les détails dans le vault Obsidian et lier ici.
