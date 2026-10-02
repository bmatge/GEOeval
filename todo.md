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
- [ ] Lot 1.3b — API v1 `/api/v1/orgs/{slug}/…` : lecture + lancement + jobs, jetons d'organisation (`api_tokens`), problem+json, OpenAPI
- [ ] Lot 1.3c — API v1 écriture : questions, périmètres, planifications
- [ ] Lot 1.4 — Alembic hors démarrage
- [ ] Lot 1.5 — logs JSON, `/healthz`, `/readyz`, métriques
- [ ] Lot 1.6 — DSFR / Chart.js vendorisés
- [ ] Lot 2 — ProConnect, Nexus/Jenkins, secrets, manifests (après réponses Nubo : socle imposé ? egress LLM ?)

## Échelle — ADR-089 (modèle cible ministériel / interministériel)
Voir `docs/adr/ADR-089-modele-cible-echelle-ministerielle.md`. Arbitrages actés : parent_id + chemin
matérialisé, budget consolidé souple, rôles hérités vers le bas, pools par référence + visibilité.
- [x] PR #42 — habilitations dans l'app, promotion par groupe OIDC transitoire
- [ ] E1 — hiérarchie `organizations` (parent_id, path, kind, siret) + résolveur `effective_setting`
- [ ] E2 — rôles hérités vers le bas + délégation
- [ ] E3 — budget consolidé + alertes 80/100 %
- [ ] E4 — `question_pools` (référence, visibilité), thèmes, `perimeters.domains`
- [ ] E5 — `llm_contracts` (remplace `org_credentials`) + politique de routage
- [ ] E6 — ProConnect : rattachement proposé par `siret`, relecture à chaque connexion
- [ ] E7 — notifications + détecteur « question toujours fausse »
- [ ] E8 — campagnes, cycle de vie des questions (minimal au pilote)

## À faire
- Rendre `main.py` paramétrable en ligne de commande (argparse) plutôt que des listes en dur.
- Améliorer l'extraction de citations (utiliser les métadonnées de sources des API au lieu d'une regex).
  → couvert par EPIC-001 / S2.3 (annotations `url_citation` OpenRouter).
- Restreindre `*_RETRY_EXCEPTIONS` aux erreurs transitoires uniquement.
