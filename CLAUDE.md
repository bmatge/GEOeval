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
  déployé sur https://geoeval.lab.miweb.run (`AUTH=public` — l'app gère elle-même
  l'authentification : comptes locaux + SSO OIDC optionnel, ADR-086)
- Dépendances critiques : `openai` (aussi pour Albert / endpoints compatibles), `mistralai`
  (v2 — import via fallback `mistralai.client`), `google-genai` ; auth : `bcrypt`,
  `authlib`, `itsdangerous`

## 3. Comment lancer

```bash
# Dev local (Postgres seul)
docker compose -f docker-compose.local.yml up -d
cp .env.example .env                    # DATABASE_URL + clés API
python init_db.py && psql "$DATABASE_URL" -f seed.sql
python run_web.py                       # UI sur http://127.0.0.1:8000

# Test local du conteneur complet (ce que fait le VPS)
docker compose up -d --build            # nécessite le réseau externe `proxy` + APP_NAME/DOMAIN

# Déploiement
ssh vps "spawn up geoeval"              # clés API dans /opt/apps/geoeval/.env (survit aux pulls)
```

## 4. Structure des dossiers

```
.
├── run.py / evaluate.py   → phases RUN (web search) et ÉVALUATION (juges, JSON strict)
├── llm_clients.py         → client_for_model() (config base + repli env), retry/fail-fast
├── models.py / db.py      → ORM (7 tables + scheduled_runs) ; init_db.py + migrations.sql + seed.sql
├── webapp/                → app.py (routes), services.py (DAO), jobs.py (worker), scheduler.py (poll 30 s)
│   └── templates/         → Jinja2 DSFR (_run_selection.html partagé lancer/planifier)
├── Dockerfile + docker-entrypoint.sh   → attente db → init_db → migrations → seed → uvicorn :3000
└── main.py / mainUnitaire.py           → CLI historiques (hors UI)
```

## 5. Conventions

- Style : type hints systématiques ; docstrings courtes en français
- Branches : `master` protégé par convention, **tout passe par PR** (merge par Bertrand)
- Commits : Conventional Commits (`feat:`, `fix:`, `docs:`…), messages en français
- Migrations : jamais d'ALTER manuel — ajouter à `migrations.sql` (idempotent,
  `ADD COLUMN IF NOT EXISTS`), rejoué à chaque démarrage du conteneur

## 6. Ce que Claude doit toujours faire

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
- Relancer `spawn up geoeval --auth …` avec un autre mode : `AUTH=public` est voulu et
  sticky depuis ADR-086 (l'app porte sa propre auth — remettre un gate devant créerait
  un double login)
- Poser `DEV_FAKE_EMAIL` en prod (bypass complet de l'auth applicative)

## 8. Références externes

- Note projet dans le vault : `~/Documents/Obsidian/10-Projects/GEOeval.md`
- ADR-076 (historique inviolable, config modèles, planification) : vault `30-Knowledge/ADR/`
- Backlog : issues GitHub **désactivées** sur ce repo → suivre via PR + `todo.md`
- Proto : https://geoeval.lab.miweb.run · plateforme : ADR-038 (spawn), ADR-056 (secrets partagés)

---

⚠️ **Garder ce fichier sous 200 lignes.** Si ça dépasse, déplacer les détails dans le vault Obsidian et lier ici.
