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
| 3 | **Découpage de `webapp/app.py`** (2 176 lignes, 83 routes) en `APIRouter` par domaine | Lisibilité, revues, parallélisation du travail |
| 4 | **Alembic** à la place de `migrations.sql`, exécuté **hors démarrage** (Job / étape pipeline) | `init_db + migrations + seed` à chaque boot = course entre réplicas |
| 5 | **Observabilité** : logs JSON structurés sur stdout (request_id, job_id), `/healthz`, `/readyz`, export métriques | Inexistant aujourd'hui ; exigé par toute plateforme d'exploitation |
| 6 | **DSFR, Chart.js et dsfr-chart vendorisés** dans `webapp/static` | Chargés depuis un CDN public : incompatibles réseau fermé / CSP stricte |

### 2.2 Lot 2 — spécifique Nubo

| # | Chantier | Notes |
|---|----------|-------|
| 7 | **ProConnect** en remplacement de la double couche VibeLab (gate magic-link + comptes locaux) | Réutiliser `webapp/oidc.py` (générique). Spécificités : userinfo en JWT signé, claims `siret`, `usual_name`, `given_name`, `idp_id`, déconnexion via `end_session_endpoint`. Rattachement org possible sur `siret`. Conserver un compte local de secours. |
| 8 | **Chaîne de livraison Nexus / Jenkins** | Image de base depuis le miroir, `PIP_INDEX_URL` Nexus, dépendances figées avec hashes, SBOM, Jenkinsfile lint → tests → build → push → déploiement |
| 9 | **Secrets de plateforme** | `.env` → secrets Nubo ; procédure de rotation de `GEOEVAL_KEY_SECRET` (Fernet BYOK) |
| 10 | **Manifests de déploiement** (web, worker, migration) | Même image pour les trois rôles |

### 2.3 Risque bloquant à lever en premier : les flux sortants

Le cœur de l'application appelle **OpenAI, Mistral, Google, OpenRouter et Exa**. Sur un
cloud souverain, l'egress est filtré. Avant d'investir dans le lot 2 :

- obtenir la liste d'autorisation egress et vérifier que les SDK honorent le proxy de sortie ;
- trancher la question de l'envoi de requêtes vers des API extra-européennes depuis l'État.

Albert (Etalab) reste le juge souverain appelé en direct. La réponse à cette question pèse
plus que tout choix de framework.

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

## 5. Première PR (lot 1, chantier 1)

- `pyproject.toml` : config pytest (`pythonpath = ["."]`, marqueur `integration`) et ruff.
- `requirements-dev.txt` : pytest, ruff.
- `tests/` : tests unitaires sans base (parsing juge, retry, citations OpenRouter, kappa /
  Spearman, planification, rôles, validation gold) et tests d'intégration contre PostgreSQL
  (schéma + migrations + seed, pages principales via `TestClient`), sautés sans
  `DATABASE_URL`.
- `.github/workflows/ci.yml` : ruff + pytest (service PostgreSQL 16) + `docker build`.
