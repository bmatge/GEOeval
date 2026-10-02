"""
UI web GEOeval — FastAPI + Jinja2 + DSFR. Assemblage de l'application.

Lancement :
    uvicorn geoeval.web.app:app --reload
ou :
    python -m scripts.run_web

Lot 1.3a (ADR-088 §2.3) : ce module ne porte plus de route. Les pages HTML
sont des routers minces dans `geoeval.web.ui.*` ; les règles métier vivent
dans les services (`geoeval.web.launching`, `budget`, `org_models`, …) ; le
rendu partagé est dans `geoeval.web.rendering`. L'API v1 (`geoeval.web.api`) est
montée sur /api/v1 à côté des routers UI.

Depuis PR#12 (ADR-077) les routes de domaine sont préfixées `/o/{org_slug}/…`.
Les anciennes URL (`/runs`, `/dashboard`, …) redirigent en 301 vers l'org
primaire du user courant, ce qui préserve la compat des liens externes.
"""
from __future__ import annotations

import logging
import os
import secrets as _secrets

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from geoeval.observability.logs import configure_logging
from geoeval.observability.middleware import RequestContextMiddleware
from geoeval.web import auth_routes, ops
from geoeval.web.api import create_api_app
from geoeval.web.auth import AuthMiddleware
from geoeval.web.ui import ROUTERS
from geoeval.worker.main import inline_worker_enabled, start_inline_thread

configure_logging("web")

app = FastAPI(title="GEOeval")

# Ordre des middlewares (dernier ajouté = plus externe) : SessionMiddleware doit
# envelopper AuthMiddleware, qui lit request.session (ADR-086).
_session_secret = os.environ.get("GEOEVAL_SESSION_SECRET", "").strip()
if not _session_secret:
    _session_secret = _secrets.token_urlsafe(32)
    logging.getLogger("geoeval.web").warning(
        "GEOEVAL_SESSION_SECRET absent : secret de session éphémère généré "
        "(les sessions ne survivront pas au redémarrage — dev uniquement)."
    )
app.add_middleware(AuthMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret,
    max_age=7 * 24 * 3600,
    same_site="lax",
    https_only=os.environ.get("GEOEVAL_COOKIE_SECURE", "0").strip() in ("1", "true", "yes"),
)
app.include_router(ops.router)
app.include_router(auth_routes.router)
# Lot 1.2 : le worker et le planificateur tournent dans un processus séparé
# (python -m geoeval.worker.main). Mode inline opt-in pour le dev local.
if inline_worker_enabled():
    start_inline_thread()


@app.exception_handler(401)
async def _redirect_unauthenticated(request: Request, exc: HTTPException):
    """Visiteur anonyme sur une page HTML : redirection vers /login plutôt
    qu'un 401 JSON brut (les navigations GET uniquement)."""
    if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(f"/login?next={request.url.path}", status_code=302)
    return JSONResponse(status_code=401, content={"detail": exc.detail})


for _router in ROUTERS:
    app.include_router(_router)

# API v1 (lot 1.3b) : sous-application, docs sur /api/v1/docs, erreurs problem+json.
# Les middlewares du parent (sessions, AuthMiddleware) s'appliquent aussi à elle.
app.mount("/api/v1", create_api_app(), name="api_v1")

# Lot 1.5 : request_id, journal d'accès structuré, métriques HTTP — le plus externe.
app.add_middleware(RequestContextMiddleware)
