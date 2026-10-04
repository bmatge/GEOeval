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
python -m scripts.migrate               # alembic upgrade head + seed idempotente (remplace init_db + psql seed)
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
│   ├── core/              → run.py / evaluate.py (phases RUN et ÉVALUATION), load.py, llm_clients.py (cascade contrat → modèle → env, retry)
│   ├── db/                → session.py, models.py (41 tables, index déclarés), migrate.py + alembic/ (révisions ; 0001 = base convergente, schema_base.sql ; 0002 = hiérarchie d'entités ; 0003 = alertes budgétaires ; 0004 = pools, thèmes, domaines ; 0005 = contrats LLM, routage ; 0006 = notifications ; 0007 = cycle de vie, campagnes ; 0008 = signalements, récapitulatif, chute des citations), migrations.sql (GELÉ), seed.sql
│   ├── web/               → app.py (assemblage), ui/ (routers HTML minces), api/ (API v1 sur /api/v1 : deps jetons/session, problems RFC 9457, schemas, v1/ routers), launching.py + scheduling.py (règles : liste blanche, devis, budget, échéances), hierarchy.py (arbre d'entités, résolveur de paramètres hérités), budget.py + budget_alerts.py + mailer.py (budget consolidé, alertes 80/100 %, SMTP), pools.py + themes.py (pools partagés par référence, questions effectives, catalogue de thèmes), contracts.py + routing.py (contrats LLM hérités, blocage sans repli, politique restrictive), notifications.py + detectors.py + reports.py (notifications par rôle, email immédiat/récap/aucun, détecteurs worker, signalements), campaigns.py (protocole figé, participants, exécution planifiée, résultats comparés), tenancy.py (rôles hérités `resolve_role`, délégation `can_*`), api_tokens.py, services.py (DAO), auth* + oidc.py (identité (issuer, sub), profils generic/proconnect), tenancy… + templates/ + static/vendor/ (DSFR, Chart.js… vendorisés, MANIFEST.json)
│   ├── observability/     → logs.py (JSON/texte, request_id, job_id), middleware.py (X-Request-ID, journal d'accès, métriques HTTP), metrics.py (Prometheus), health.py
│   └── worker/            → main.py (processus worker, SIGTERM gracieux), health.py (:9100 /healthz /readyz /metrics), jobs.py (file `jobs`, SKIP LOCKED), scheduler.py (verrou advisory)
├── scripts/               → migrate, vendor_assets (--verify / --refresh), set_password, run_web (`python -m scripts.<nom>`) ; legacy/ = CLI historiques
├── deploy/                → docker-entrypoint.sh, docker-compose.local.yml
├── tests/ + pyproject.toml → pytest (unitaires sans base + `integration` sur PostgreSQL), ruff ; CI .github/workflows/ci.yml
├── docs/                  → adr/ (ADR-080, 088, 089), architecture.md, epics/, spikes/
└── Dockerfile + docker-compose.yml     → racine imposée par le contrat spawn ; services migrate (one-shot), web (entrypoint : attente db → uvicorn :3000), worker, db
```

## 5. Conventions

- Style : type hints systématiques ; docstrings courtes en français
- Branches : `master` protégé par convention, **tout passe par PR** (merge par Bertrand)
- Commits : Conventional Commits (`feat:`, `fix:`, `docs:`…), messages en français
- Migrations (lot 1.4) : jamais d'ALTER manuel, jamais de modification de `migrations.sql` (gelé).
  Modifier `models.py`, puis `alembic -c geoeval/db/alembic.ini revision --autogenerate -m "…"`,
  relire la révision, `python -m scripts.migrate`. Le test `test_aucune_derive_orm_base` échoue si
  models.py et la base divergent. Les migrations s'exécutent dans le service `migrate` AVANT web et
  worker, jamais au démarrage du web.

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
  noms de variables uniquement, jamais afficher les valeurs) — y compris `GEOEVAL_SMTP_PASSWORD`
- Relancer `spawn up geoeval --auth …` avec un autre mode : `AUTH=link` est voulu depuis
  le 2026-07-30 (gate magic-link + auth applicative derrière — double login assumé).
  ⚠️ Le SSO applicatif dépend de l'application Authentik `geoeval-sso` (provider
  `geoeval-oidc`) : le slug `geoeval` appartient à `app-auth.py` (bypass gate ADR-062)
- Poser `DEV_FAKE_EMAIL` en prod (bypass complet de l'auth applicative)
- Référencer un CDN dans un gabarit : les ressources front sont vendorisées (`python -m scripts.vendor_assets --refresh`
  pour monter de version), un test refuse toute URL externe de ressource

## 8. Références externes

- Note projet dans le vault : `~/Documents/Obsidian/10-Projects/GEOeval.md`
- ADR-076 (historique inviolable, config modèles, planification) : vault `30-Knowledge/ADR/`
- ADR-088 (stack conservée, API first, refacto en 2 lots vers Nubo) · ADR-089 (hiérarchie d'entités,
  budgets consolidés, pools, contrats LLM, ProConnect) : `docs/adr/` · schémas : `docs/architecture.md`
- Backlog : issues GitHub **désactivées** sur ce repo → suivre via PR + `todo.md`
- Sondes : `/healthz` `/readyz` `/metrics` (web) et `:9100` (worker) · logs JSON sur stdout (`GEOEVAL_LOG_FORMAT`)
- Proto : https://geoeval.lab.miweb.run · API v1 : `/api/v1/docs` (jeton `Authorization: Bearer`, créé dans Paramètres de l'org) · plateforme : ADR-038 (spawn), ADR-056 (secrets partagés)

---

⚠️ **Garder ce fichier sous 200 lignes.** Si ça dépasse, déplacer les détails dans le vault Obsidian et lier ici.
