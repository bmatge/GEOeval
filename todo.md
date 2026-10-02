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
Voir `docs/adr/ADR-088-stack-conservee-refacto-incremental-nubo.md`.
- [x] Lot 1.1 — tests + CI (pytest, ruff, GitHub Actions, docker build)
- [ ] Lot 1.2 — worker hors du processus web (jobs en base, `SKIP LOCKED`, SIGTERM)
- [ ] Lot 1.3 — découpage de `webapp/app.py` en routers
- [ ] Lot 1.4 — Alembic hors démarrage
- [ ] Lot 1.5 — logs JSON, `/healthz`, `/readyz`, métriques
- [ ] Lot 1.6 — DSFR / Chart.js vendorisés
- [ ] Lot 2 — ProConnect, Nexus/Jenkins, secrets, manifests (après réponses Nubo : socle imposé ? egress LLM ?)

## À faire
- Rendre `main.py` paramétrable en ligne de commande (argparse) plutôt que des listes en dur.
- Améliorer l'extraction de citations (utiliser les métadonnées de sources des API au lieu d'une regex).
  → couvert par EPIC-001 / S2.3 (annotations `url_citation` OpenRouter).
- Restreindre `*_RETRY_EXCEPTIONS` aux erreurs transitoires uniquement.
