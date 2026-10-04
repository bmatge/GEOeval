# API v1 — guide

L'API de GEOeval est montée sur `/api/v1`. L'interface web et l'API appellent les mêmes services :
tout ce que fait l'interface est faisable par l'API (ADR-088 §2.3, « API first »).

| Quoi | Où |
|---|---|
| Documentation interactive (Swagger UI) | `/api/v1/docs` — lien « Documentation de l'API » en pied de page |
| Schéma OpenAPI 3.1 de l'instance | `/api/v1/openapi.json` |
| Schéma versionné dans le dépôt | [`docs/openapi.json`](openapi.json) |

Swagger UI est **vendorisé** (`geoeval/web/static/vendor/swagger-ui-<version>/`) : la page fonctionne
sur un réseau fermé, sans aucun appel à un CDN.

## S'authentifier

1. Un `org_admin` crée un **jeton d'organisation** dans *Paramètres* de l'entité (ou par
   `POST /api/v1/orgs/{org_slug}/tokens`). Le jeton porte un rôle (`viewer`, `editor`, `org_admin`)
   et n'est affiché qu'une fois.
2. Il se passe en en-tête : `Authorization: Bearer geoeval_…`.

```bash
export GEOEVAL=https://geoeval.lab.miweb.run/api/v1
curl -s -H "Authorization: Bearer $GEOEVAL_TOKEN" $GEOEVAL/me
```

Dans Swagger UI, le bouton **Authorize** enregistre le jeton pour tous les essais. Sans jeton, la page
utilise la session du navigateur si tu es connecté à l'interface.

Les évaluations et statistiques sont lisibles **sans authentification**. À l'inverse, quelques actions
sont personnelles et exigent une **session** (un jeton n'a pas d'identité) : notifications (`/me/…`),
annotation de calibration, approbation ou renvoi d'une question en relecture.

## Rôles

`viewer` (lecture) < `editor` (corpus, lancements, rejugement) < `org_admin` (membres, jetons, budget,
contrats, réglages, campagnes, promotion d'un lot). Un rôle posé sur une entité vaut pour tout son
sous-arbre. Le rôle requis figure dans le résumé de chaque route.

## Parcours type : lancer une évaluation et lire ses notes

```bash
ORG=mon-entite
# 1. Périmètres et questions prêtes
curl -s -H "Authorization: Bearer $GEOEVAL_TOKEN" $GEOEVAL/orgs/$ORG/perimeters
curl -s -H "Authorization: Bearer $GEOEVAL_TOKEN" $GEOEVAL/orgs/$ORG/perimeters/1/effective-questions

# 2. Lancement (editor+) : contrôles (liste blanche, routage, contrats, budget), puis mise en file
curl -s -X POST -H "Authorization: Bearer $GEOEVAL_TOKEN" -H "Content-Type: application/json" \
  -d '{"perimeter_id": 1, "tested_models": ["mistralai/mistral-large-2512"], "judge_models": ["openweight-large"], "test_ids": [1, 2]}' \
  $GEOEVAL/orgs/$ORG/runs            # 202 + job

# 3. Suivi du job, puis lecture des runs
curl -s -H "Authorization: Bearer $GEOEVAL_TOKEN" $GEOEVAL/orgs/$ORG/jobs/<job_id>
curl -s $GEOEVAL/orgs/$ORG/runs
```

## Erreurs

Format `application/problem+json` (RFC 9457) : `type`, `title`, `status`, `detail`, `instance`,
plus des champs propres aux refus de lancement (`forbidden`, `estimate_eur`, `problems`).

| Statut | Cas |
|---|---|
| 400 | sélection ou paramètre invalide |
| 401 | jeton absent, invalide, expiré ou révoqué |
| 402 | plafond budgétaire atteint |
| 403 | rôle insuffisant, modèle non autorisé, politique de routage |
| 404 | ressource introuvable ou hors de l'entité |
| 409 | état incompatible (contrat échu, protocole figé, lot non terminé…) |
| 422 | corps ou paramètres mal formés |

## Groupes de routes

Les 18 groupes (organisations, périmètres, questions, validation métier, pools, thèmes, modèles,
évaluations, jobs, planifications, campagnes, rejugement et calibration, statistiques, signalements,
notifications, budget, contrats LLM, jetons) sont décrits dans `geoeval/web/api/__init__.py` (`TAGS`)
et affichés dans cet ordre par Swagger UI.

## Faire évoluer l'API

- Chaque route porte un `summary` (avec le rôle requis) et un `tags` déclaré dans `TAGS`.
- Après tout changement de route ou de schéma : `python -m scripts.export_openapi`, puis relire le
  diff de `docs/openapi.json` dans la PR. C'est le contrat.
- `tests/test_api_docs_unit.py` échoue si une route n'est pas documentée, si un groupe n'est pas
  déclaré, si la page de documentation référence une ressource externe, ou si `docs/openapi.json`
  n'est plus à jour. `python -m scripts.export_openapi --check` fait la même vérification en ligne de commande.
- Monter Swagger UI de version : modifier `PACKAGES` dans `scripts/vendor_assets.py`, lancer
  `python -m scripts.vendor_assets --refresh`, puis mettre à jour `SWAGGER_UI` dans `geoeval/web/api/__init__.py`.
