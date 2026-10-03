# TODO

## Fait
1. [x] Corriger la signature de `evaluate_run`
   => `evaluate_run(session, run_id, judges=[{"model_id": 2, "repeats": 2}])`

2. [x] Appel des juges/modèles par nom de modèle
   => judges=[
          {"model": "gpt-5.2", "repeats": 2},
          {"model": "gpt-4.1-mini", "repeats": 1},
      ]
   La conversion "gpt-5.2" -> model_id se fait en interne (table `models`, via `resolve_model`).
   `execute_run` accepte aussi un nom OU un id pour le modèle testé.

## Epics
- **EPIC-001 — Bascule OpenRouter (provider plateforme unique)** : voir
  `docs/epics/EPIC-001-openrouter-multitenant.md` (cadre :
  `docs/adr/ADR-080-openrouter-provider-unique-websearch.md`).
  Statut : Proposé. Prochaine étape = Phase 0 (spike geo-targeting FR + Mistral natif/Exa).

## Pilote — ADR-088 (stack conservée, refacto incrémental vers Nubo)
Voir `docs/adr/ADR-088-stack-conservee-refacto-incremental-nubo.md` et `docs/architecture.md`.
- [x] Lot 1.1 — tests + CI (pytest, ruff, GitHub Actions, docker build)
- [x] Lot 1.1b — réorganisation du dépôt en package `geoeval/` (ADR-088 §2.5)
- [x] Lot 1.2 — worker hors du processus web (tables `jobs`/`job_logs`, `SKIP LOCKED`, scheduler sous verrou, SIGTERM gracieux, service `worker` du compose)
- [x] Lot 1.3a — `geoeval/web/ui/` routers HTML par domaine, `rendering.py`, service `launching.py` (budget + liste blanche hors des contrôleurs, run-now soumis au budget)
- [x] Lot 1.3b — API v1 `/api/v1/orgs/{slug}/…` : lecture + lancement + jobs, jetons d'organisation (`api_tokens`), problem+json, OpenAPI `/api/v1/docs`
- [x] Lot 1.3c — API v1 écriture : périmètres (POST/PATCH/DELETE), questions (POST/PATCH, désactivation, vérité de référence versionnée), planifications (POST/PATCH/DELETE) ; service `scheduling.py`
- [x] Lot 1.4 — Alembic : révision 0001 convergente (base vierge → schema_base.sql, base existante → migrations.sql gelé), service `migrate` one-shot avant web et worker, garde-fou de dérive ORM/base en CI
- [x] Lot 1.5 — logs JSON (request_id, job_id), `/healthz` `/readyz` `/metrics` web et worker (:9100), métriques Prometheus HTTP / jobs / LLM, healthchecks compose
- [x] Lot 1.6 — DSFR, Chart.js, dsfr-chart, dsfr-data vendorisés (`geoeval/web/static/vendor`, manifeste SHA-256, `scripts.vendor_assets`) — **lot 1 terminé**
- [ ] Lot 2 — ProConnect, Nexus/Jenkins, secrets, manifests (après réponses Nubo : socle imposé ? egress LLM ?)

## Échelle — ADR-089 (modèle cible ministériel / interministériel)
Voir `docs/adr/ADR-089-modele-cible-echelle-ministerielle.md`. Arbitrages actés : parent_id + chemin
matérialisé, budget consolidé souple, rôles hérités vers le bas, pools par référence + visibilité.
- [x] PR #42 — habilitations dans l'app, promotion par groupe OIDC transitoire
- [x] E1 — hiérarchie `organizations` (révision 0002 : parent_id, kind, path, depth, siret) + résolveur (`hierarchy.resolve_nearest` / `resolve_restrictive`) ; liste blanche héritée par intersection ; UI admin arbre + API `POST/PATCH /orgs`
- [x] E2 — rôles hérités vers le bas (utilisateurs et jetons, `EffectiveRole` ancré), délégation de structure dans le sous-arbre (UI paramètres › sous-entités, API `POST/PATCH /orgs`), liste blanche bornée au-dessus de l'ancre, membres hérités affichés
- [x] E3 — budget consolidé dans l'arbre (plafond = entité + sous-arbre, refus si un plafond de la chaîne est dépassé), alertes 80/100 % dans l'app et par email (SMTP stdlib, `budget_alerts`, révision 0003), planificateur qui saute et trace, API `GET /orgs/{slug}/budget`, jauges Prometheus
- [x] E4 — pools de questions partagés par référence (visibilité privé / sous-arbre / tous, inclusions sans cycle, abonnement à un périmètre), catalogue global de thèmes (admin plateforme), domaines officiels des périmètres + part des citations officielles (révision 0004), UI + API v1
- [x] E5 — contrats LLM hérités (famille ± modèles, validité, plafond, blocage sans repli), BYOK converties (révision 0005), politique de routage restrictive (fournisseurs ; notateurs souverains / UE), imputation `usage.contract_id`, UI + API v1
- [ ] Révision ultérieure : supprimer `org_credentials` (gelée depuis E5)
- [x] E6 (minimal) — identité OIDC par `(issuer, sub)` : l'email suit les mutations, anti-takeover, migration entre fournisseurs ; profils `OIDC_PROFILE` (generic / proconnect) et correspondance des claims
- [ ] E6 (branchement) — rattachement proposé par `siret` + validation org_admin, relecture du `siret` à chaque connexion, `userinfo` JWT ProConnect, inscription du client
- [x] E7 (socle) — notifications in-app + email selon préférences (révision 0006), budget 80/100 % raccordé, évaluation en échec, contrat expirant (J-30, J-7) / expiré, détecteur « question toujours fausse » (N et seuil hérités), UI + API v1
- [x] E7 (suite) — signalements humains (tout membre signale, le propriétaire traite), chute des citations officielles (écart en points hérité), emails immédiat / récapitulatif quotidien / aucun par type (révision 0008), UI + API v1
- [ ] E7 (fin) — webhooks (Tchap), abonnements partagés
- [x] E8 (minimal) — cycle de vie brouillon / publiée / retirée ; campagnes à participants désignés, protocole figé à l'activation, exécution planifiée par participant (sautée et tracée si refusée), grilles verrouillées, comparaison participants × IA (révision 0007), UI + API v1
- [ ] E8 (complet) — validation métier des questions, rejugement versionné, calibration des juges

## À faire
- Rendre `main.py` paramétrable en ligne de commande (argparse) plutôt que des listes en dur.
- Améliorer l'extraction de citations (utiliser les métadonnées de sources des API au lieu d'une regex).
  → couvert par EPIC-001 / S2.3 (annotations `url_citation` OpenRouter).
- Restreindre `*_RETRY_EXCEPTIONS` aux erreurs transitoires uniquement.
