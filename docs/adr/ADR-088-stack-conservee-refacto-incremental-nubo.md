# ADR-088 — Stack conservée, refacto incrémental vers le pilote (cible Nubo)

- **Statut** : Acceptée (2026-10-02)
- **Décideur** : Bertrand Matge
- **Contexte amont** : ADR-076 (historique inviolable), ADR-080 (OpenRouter), ADR-086 (auth
  applicative + OIDC), ADR-087 (lecture publique)
- **Suivi** : `todo.md` + PR par lot (issues GitHub désactivées)

## 1. Contexte

Le POC GEOeval est validé pour passer en **pilote**, avec une possible montée à l'échelle.
Deux faits structurent la décision :

1. **Le code n'est plus un petit POC.** ~7 700 lignes de Python, 23 tables, multi-tenant
   avec RBAC, BYOK chiffré, budgets, OIDC générique, audit log. Réécrire coûterait des mois
   pour zéro fonctionnalité nouvelle.
2. **La cible d'hébergement change.** Le POC tourne sur VibeLab (spawn, Traefik, gate
   magic-link, `.env` sur VPS). Le pilote vise le **cloud Nubo**, avec son outillage
   (Nexus, Jenkins), **ProConnect** pour l'authentification, et des exigences
   d'exploitation (workers, logs, sondes, secrets) qu'un POC peut négliger.

La question posée : garder la stack, changer de stack, ou refactoriser ?

## 2. Décision

**Conserver la stack applicative** (Python 3.12, FastAPI, SQLAlchemy 2, PostgreSQL 16,
Jinja2 + DSFR) et **refactoriser par incréments**, en deux lots, sans réécriture.

Rien dans la stack ne bloque Nubo. Ce qui doit changer est *autour* du code : livraison,
authentification, modèle d'exécution, observabilité, configuration. Une réécriture dans
un autre langage ou framework n'est justifiée que si Nubo **impose un socle technique**
incompatible avec Python ; c'est une question à poser à l'équipe Nubo avant le lot 2.

### 2.1 Lot 1 — fondations indépendantes de la cible (utiles aussi sur VibeLab)

| # | Chantier | Pourquoi maintenant |
|---|----------|---------------------|
| 1 | **Tests + CI** (pytest, ruff, GitHub Actions, build Docker) | Prérequis à tout refacto ; aucune couverture aujourd'hui |
| 2 | **Worker hors du processus web** : jobs persistés en base, processus `worker` séparé, verrou `SELECT … FOR UPDATE SKIP LOCKED`, arrêt gracieux SIGTERM | File en mémoire + thread unique : jobs perdus au redémarrage, scheduler dupliqué dès 2 réplicas |
| 3 | **Découpage API / UI de `geoeval/web/app.py`** (2 176 lignes, 83 routes) : `geoeval/web/api/v1` (JSON, Pydantic, OpenAPI) + `geoeval/web/ui` (HTML minces), règles rapatriées dans les services, jetons d'organisation pour les clients machine | Lisibilité ; et surtout **API first** (§2.4) |
| 4 | **Alembic** à la place de `migrations.sql`, exécuté **hors démarrage** — livré (lot 1.4) : révision 0001 **convergente** (base vierge → instantané pg_dump figé ; base existante → migrations.sql gelé), service `migrate` one-shot dont web et worker dépendent, garde-fou de dérive ORM/base en CI | `init_db + migrations + seed` à chaque boot = course entre réplicas |
| 5 | **Observabilité** : logs JSON structurés sur stdout (request_id, job_id), `/healthz`, `/readyz`, export métriques — livré (lot 1.5) : `prometheus-client` retenu (pure Python, standard de fait, pont OpenTelemetry possible), mini serveur HTTP interne pour le worker | Inexistant au POC ; exigé par toute plateforme d'exploitation |
| 6 | **DSFR, Chart.js et dsfr-chart vendorisés** dans `geoeval/web/static` | Chargés depuis un CDN public : incompatibles réseau fermé / CSP stricte |

### 2.2 Lot 2 — spécifique Nubo

| # | Chantier | Notes |
|---|----------|-------|
| 7 | **ProConnect** en remplacement de la double couche VibeLab (gate magic-link + comptes locaux) | Réutiliser `geoeval/web/oidc.py` (générique). Spécificités : userinfo en JWT signé, claims `siret`, `usual_name`, `given_name`, `idp_id`, déconnexion via `end_session_endpoint`. Rattachement org possible sur `siret`. Conserver un compte local de secours. |
| 8 | **Chaîne de livraison Nexus / Jenkins** | Image de base depuis le miroir, `PIP_INDEX_URL` Nexus, dépendances figées avec hashes, SBOM, Jenkinsfile lint → tests → build → push → déploiement |
| 9 | **Secrets de plateforme** | `.env` → secrets Nubo ; procédure de rotation de `GEOEVAL_KEY_SECRET` (Fernet BYOK) |
| 10 | **Manifests de déploiement** (web, worker, migration) | Même image pour les trois rôles |

### 2.3 Principe API first (amendement 2026-10-02)

GEOeval n'est pas API first aujourd'hui : 6 routes JSON en lecture, 94 paramètres de
formulaire, aucun schéma Pydantic. La stack est pourtant la bonne pour le devenir : FastAPI
est un framework d'API, et la couche services est déjà séparée du rendu.

Règles adoptées :

1. **Toute fonctionnalité existe d'abord dans `geoeval/web/api/v1`** (schémas Pydantic en entrée
   et sortie, OpenAPI exposé et versionné, pagination, format d'erreur unique).
2. **L'UI ne peut rien faire que l'API ne permette pas**, mais **n'appelle pas l'API en
   HTTP** : API et UI partagent la couche services (pas de double auth ni de latence).
3. **Toutes les règles vivent dans les services**, jamais dans un contrôleur. Constat
   initial : le contrôle budget et la liste blanche des modèles étaient dans `app.py` ; une API
   les aurait contournés. Corrigé par le lot 1.3a (`geoeval/web/launching.py`, routers UI
   minces dans `geoeval/web/ui/`). Arbitrages 1.3 : trois PR (1.3a découpage + service,
   1.3b API v1 lecture + lancement + jetons, 1.3c API v1 écriture), jetons d'organisation en
   base, erreurs RFC 9457 problem+json, préfixe `/api/v1/orgs/{slug}/…`.
4. **Auth machine** : jetons porteurs par organisation, mêmes trois rôles, révocables.
5. **Pas de SPA** : le back-office DSFR rendu côté serveur reste, il consomme les mêmes
   services.

Ce que ça rapporte au pilote : runs lancés depuis Jenkins ou un cron externe, export des
scores vers Grist ou data.gouv, intégration dans d'autres outils, et un front séparé
possible plus tard sans réécriture.

Schémas d'architecture (existant, cible, déroulé d'un run, règles, routes LLM) :
[`docs/architecture.md`](../architecture.md).

### 2.4 Risque bloquant à lever en premier : les flux sortants

Le cœur de l'application appelle **OpenAI, Mistral, Google, OpenRouter et Exa**. Sur un
cloud souverain, l'egress est filtré. Avant d'investir dans le lot 2 :

- obtenir la liste d'autorisation egress et vérifier que les SDK honorent le proxy de sortie ;
- trancher la question de l'envoi de requêtes vers des API extra-européennes depuis l'État.

Albert (Etalab) reste le juge souverain appelé en direct. La réponse à cette question pèse
plus que tout choix de framework.

### 2.5 Organisation du dépôt (amendement 2026-10-02)

La racine du POC mélangeait modules métier, scripts, SQL et fichiers de déploiement.
Arbitrage : **package `geoeval/` hiérarchisé**, déplacement en PR dédiée juste après la
PR tests + CI, CLI historiques conservés dans `scripts/legacy/`.

```
geoeval/            package applicatif (une seule racine d'imports)
  core/             run, evaluate, load, llm_clients       — cœur benchmark, sans FastAPI
  db/               session, models, migrations.sql, seed.sql
  web/              app, services, auth, tenancy, …, templates/   → futur api/ + ui/
  worker/           jobs, scheduler                         → futur processus worker (lot 1.2)
scripts/            init_db, set_password, run_web ; legacy/ (main, mainUnitaire)
deploy/             docker-entrypoint.sh, docker-compose.local.yml
docs/  tests/       documentation (ADR, schémas) et tests
Dockerfile, docker-compose.yml   restent à la racine : contrat spawn VibeLab (ADR-038)
```

Écartés : nettoyage léger (la racine reste un fourre-tout de modules) et layout
`src/geoeval` installable (étape d'installation en plus pour un gain faible sur une
application non publiée). Les scripts s'exécutent depuis la racine par
`python -m scripts.<nom>`, ce qui évite tout bricolage de `sys.path`.

## 3. Alternatives écartées

- **Réécriture Django** : admin, auth et migrations offerts, mais tout cela existe déjà dans
  GEOeval. Coût de réécriture > gain.
- **Réécriture Java / Node pour « faire Nubo »** : aucune exigence connue ne l'impose. À
  reconsidérer uniquement si la plateforme impose un socle.
- **Front JS séparé** : un back-office DSFR rendu côté serveur suffit ; un front séparé
  ajoute une compétence d'équipe sans gain fonctionnel.
- **Celery / Redis pour les jobs** : surdimensionné. Les runs sont de toute façon
  sérialisés par les quotas des API LLM ; PostgreSQL suffit comme file.

## 4. Conséquences

- VibeLab reste l'environnement de **développement et préproduction**. La **même image
  Docker** doit tourner sur VibeLab et sur Nubo : le Dockerfile est le contrat commun.
- Chaque chantier tient en une PR et **ne touche jamais à l'historique des runs** (ADR-076).
- `CLAUDE.md` est mis à jour au fil des lots (il annonce 7 tables, il y en a 23).
- Cette ADR est amendée quand les réponses Nubo (socle imposé, egress) sont connues.
- Amendement 2026-10-02 : principe API first (§2.3), schémas `docs/architecture.md`, organisation du dépôt (§2.5).

## 5. Première PR (lot 1, chantier 1)

- `pyproject.toml` : config pytest (`pythonpath = ["."]`, marqueur `integration`) et ruff.
- `requirements-dev.txt` : pytest, ruff.
- `tests/` : tests unitaires sans base (parsing juge, retry, citations OpenRouter, kappa /
  Spearman, planification, rôles, validation gold) et tests d'intégration contre PostgreSQL
  (schéma + migrations + seed, pages principales via `TestClient`), sautés sans
  `DATABASE_URL`.
- `.github/workflows/ci.yml` : ruff + pytest (service PostgreSQL 16) + `docker build`.
