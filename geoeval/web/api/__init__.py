"""
API v1 de GEOeval (ADR-088 §2.3 — API first).

Sous-application FastAPI montée sur ``/api/v1`` : schéma OpenAPI sur
``/api/v1/openapi.json``, documentation interactive (Swagger UI vendorisé, aucun CDN)
sur ``/api/v1/docs``, erreurs RFC 9457 problem+json, authentification par jeton
d'organisation (``Authorization: Bearer``) ou par session navigateur. L'API et l'UI
appellent les mêmes services : aucune règle ne vit dans ces routers.

Le schéma est aussi versionné dans ``docs/openapi.json`` (``python -m scripts.export_openapi``) ;
un test échoue s'il n'est plus à jour ou si une route n'est pas documentée.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse

from geoeval.web.api.problems import PROBLEM_MEDIA_TYPE, install_handlers
from geoeval.web.api.v1 import ROUTERS

API_VERSION = "1.0"
MOUNT_PATH = "/api/v1"
SWAGGER_UI = "/static/vendor/swagger-ui-5.33.1"

API_DESCRIPTION = """
API du benchmark GEOeval : des IA avec recherche web répondent à des questions factuelles,
des notateurs évaluent la réponse et les citations, et l'historique permet de suivre l'évolution.
L'interface web et l'API appellent les mêmes services : tout ce que fait l'interface est faisable ici.

## Ressources

Presque toutes les ressources sont rattachées à une **entité** (organisation) : `/orgs/{org_slug}/…`.
Les entités forment un arbre (ministère › direction › service). Les rôles, budgets, contrats LLM,
réglages et pools partagés s'héritent le long de cet arbre.

## Authentification

- **Jeton d'organisation** : en-tête `Authorization: Bearer geoeval_…`. Il est créé par un `org_admin`
  dans *Paramètres* de l'entité (ou via `POST /orgs/{org_slug}/tokens`), porte un rôle et ne vaut que
  pour cette entité. Bouton **Authorize** ci-dessous pour l'essayer.
- **Session navigateur** : si tu es connecté à l'interface, les appels depuis cette page utilisent ta session.
- **Lecture publique** : les évaluations et statistiques sont lisibles sans authentification.

Quelques actions sont personnelles et exigent une **session** (un jeton n'a pas d'identité) :
notifications (`/me/…`), annotation de calibration, approbation ou renvoi d'une question en relecture.

## Rôles

`viewer` (lecture) < `editor` (corpus, lancements, rejugement) < `org_admin` (membres, jetons, budget,
contrats, réglages, campagnes, promotion d'un lot). Un rôle posé sur une entité vaut pour tout son sous-arbre.
Le rôle requis figure dans le résumé de chaque route.

## Erreurs

Toutes les erreurs sont au format `application/problem+json` (RFC 9457) : `type`, `title`, `status`,
`detail`, `instance`. Les refus de lancement ajoutent des champs (`forbidden`, `estimate_eur`, `problems`) :

| Statut | Cas |
|---|---|
| 400 | sélection ou paramètre invalide |
| 401 | jeton absent, invalide, expiré ou révoqué |
| 402 | plafond budgétaire atteint (mensuel ou journalier, consolidé sur l'arbre) |
| 403 | rôle insuffisant, modèle non autorisé, politique de routage |
| 404 | ressource introuvable ou hors de l'entité |
| 409 | état incompatible (contrat échu, protocole figé, lot non terminé…) |
| 422 | corps ou paramètres mal formés |

## Conventions

- Lancer une évaluation ou un rejugement renvoie un **job** (`202` ou `201`) : son avancement se lit sur
  `/orgs/{org_slug}/jobs/{job_id}`.
- **Jamais de suppression** de question, de modèle ou d'évaluation référencés : on désactive ou on retire,
  l'historique est conservé.
- Les notes vont de 0 à 10. Les dates sont en ISO 8601, en UTC.
"""

TAGS = [
    ("organisations", "Entités, arbre hiérarchique, profil de l'appelant (`/me`)."),
    ("périmètres", "Contexte de recherche d'une entité : un site ou une thématique, ses domaines officiels, "
                   "ses pools abonnés et ses questions effectives."),
    ("questions", "Corpus de l'entité : énoncé, réponse attendue, vérité de référence versionnée, cycle de vie "
                  "(brouillon, en relecture, publiée, retirée)."),
    ("validation métier", "Relecture à quatre yeux avant publication : réglage hérité, validateurs, "
                          "approbation ou renvoi d'une question."),
    ("pools", "Jeux de questions partagés par référence entre entités (visibilité privée, sous-arbre ou tous)."),
    ("thèmes", "Catalogue global de thèmes pour classer questions et périmètres."),
    ("modèles", "Catalogue des IA évaluées et des notateurs, et liste blanche de l'entité."),
    ("évaluations", "Lancement d'une évaluation (devis, contrôles, mise en file) et lecture des runs et de leurs notes."),
    ("jobs", "File d'exécution : état, progression et journal d'un lancement."),
    ("planifications", "Évaluations programmées (une fois, quotidien, hebdomadaire, toutes les N heures)."),
    ("campagnes", "Protocole commun figé à l'activation et exécuté par des entités participantes ; résultats comparés."),
    ("rejugement et calibration", "Rejuger des runs passés avec d'autres notateurs, comparer, promouvoir un lot ; "
                                  "annotations humaines et accord des notateurs."),
    ("statistiques", "Agrégats pour les tableaux de bord : scores par IA, par question, dans le temps."),
    ("signalements", "Signaler une question douteuse à l'entité qui la possède, et traiter les signalements reçus."),
    ("notifications", "Boîte de réception personnelle, préférences d'email et réglages des détecteurs."),
    ("budget", "Plafonds mensuel et journalier, dépense consolidée sur l'arbre, alertes."),
    ("contrats LLM", "Clés et contrats fournisseurs par entité, hérités ; politique de routage."),
    ("jetons", "Jetons d'API de l'entité (création, révocation)."),
]

_PROBLEM_SCHEMA = {
    "type": "object",
    "title": "Problem",
    "description": "Erreur au format RFC 9457. Des champs supplémentaires peuvent préciser un refus de lancement.",
    "properties": {
        "type": {"type": "string", "example": "about:blank"},
        "title": {"type": "string", "example": "Ressource introuvable"},
        "status": {"type": "integer", "example": 404},
        "detail": {"type": "string", "example": "Organisation introuvable."},
        "instance": {"type": "string", "example": "/api/v1/orgs/inconnue/perimeters"},
    },
    "required": ["title", "status"],
    "additionalProperties": True,
}


def _problem(description: str) -> dict:
    return {"description": description, "content": {PROBLEM_MEDIA_TYPE: {"schema": _PROBLEM_SCHEMA}}}


COMMON_RESPONSES = {
    401: _problem("Jeton absent, invalide, expiré ou révoqué."),
    403: _problem("Rôle insuffisant pour cette entité, ou action réservée à une session."),
    404: _problem("Ressource introuvable ou hors de l'entité."),
    422: _problem("Corps ou paramètres de la requête mal formés."),
}


def create_api_app() -> FastAPI:
    api = FastAPI(
        title="GEOeval API",
        version=API_VERSION,
        summary="Benchmark longitudinal d'IA avec recherche web : corpus, évaluations, résultats.",
        description=API_DESCRIPTION,
        openapi_tags=[{"name": name, "description": description} for name, description in TAGS],
        servers=[{"url": MOUNT_PATH, "description": "Cette instance"}],
        responses=COMMON_RESPONSES,
        docs_url=None,          # page servie ci-dessous avec le Swagger UI vendorisé (aucun CDN)
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    install_handlers(api)
    for router in ROUTERS:
        api.include_router(router)

    @api.get("/docs", include_in_schema=False, response_class=HTMLResponse)
    def swagger_ui() -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url=f"{MOUNT_PATH}/openapi.json",
            title="GEOeval API — documentation",
            swagger_js_url=f"{SWAGGER_UI}/swagger-ui-bundle.js",
            swagger_css_url=f"{SWAGGER_UI}/swagger-ui.css",
            swagger_favicon_url=f"{SWAGGER_UI}/favicon-32x32.png",
            swagger_ui_parameters={
                "docExpansion": "none",            # 18 groupes : repliés par défaut
                "filter": True,                    # champ de recherche par groupe
                "persistAuthorization": True,      # le jeton survit au rechargement de la page
                "displayRequestDuration": True,
                "defaultModelsExpandDepth": 0,
                "tagsSorter": None,                # ordre de TAGS, pas l'ordre alphabétique
            },
        )

    return api
