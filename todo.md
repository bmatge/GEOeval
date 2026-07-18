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

## À faire
- Rendre `main.py` paramétrable en ligne de commande (argparse) plutôt que des listes en dur.
- Améliorer l'extraction de citations (utiliser les métadonnées de sources des API au lieu d'une regex).
  → couvert par EPIC-001 / S2.3 (annotations `url_citation` OpenRouter).
- Restreindre `*_RETRY_EXCEPTIONS` aux erreurs transitoires uniquement.
