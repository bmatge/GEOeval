"""
API v1 de GEOeval (ADR-088 §2.3 — API first, lot 1.3b).

Sous-application FastAPI montée sur ``/api/v1`` : documentation OpenAPI sur
``/api/v1/docs`` et ``/api/v1/openapi.json``, erreurs RFC 9457 problem+json,
authentification par jeton d'organisation (``Authorization: Bearer``) ou par
session navigateur. L'API et l'UI appellent les mêmes services : aucune règle
ne vit dans ces routers.

Périmètre 1.3b : lecture (organisations, périmètres, questions, modèles,
évaluations, statistiques, planifications, jobs), lancement d'une évaluation,
exécution immédiate d'une planification, gestion des jetons. L'écriture du
corpus (questions, périmètres, planifications) arrive au lot 1.3c.
"""
from __future__ import annotations

from fastapi import FastAPI

from geoeval.web.api.problems import install_handlers
from geoeval.web.api.v1 import ROUTERS

API_DESCRIPTION = """
API du benchmark GEOeval. Les ressources sont rattachées à une organisation : `/orgs/{slug}/…`.

**Authentification** : jeton d'organisation en en-tête `Authorization: Bearer geoeval_…`
(créé par un org_admin dans les paramètres de l'organisation), ou session navigateur.
Les évaluations et statistiques sont lisibles sans authentification (lecture publique).

**Erreurs** : `application/problem+json` (RFC 9457) — `type`, `title`, `status`, `detail`,
`instance`, plus des extensions (`forbidden`, `estimate_eur`) sur les refus de lancement.
"""


def create_api_app() -> FastAPI:
    api = FastAPI(
        title="GEOeval API",
        version="1.0",
        description=API_DESCRIPTION,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    install_handlers(api)
    for router in ROUTERS:
        api.include_router(router)
    return api
