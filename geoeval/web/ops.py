"""Sondes et métriques du service web (hors OpenAPI, sans authentification applicative)."""
from __future__ import annotations

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse

from geoeval.observability import health, metrics

router = APIRouter(include_in_schema=False)


@router.get("/healthz")
def healthz():
    """Vivacité : le processus répond."""
    return {"status": "ok", "service": "web"}


@router.get("/readyz")
def readyz():
    """Disponibilité : base joignable et schéma présent. 503 sinon."""
    ok, detail = health.check_db()
    return JSONResponse(status_code=200 if ok else 503, content={"status": "ok" if ok else "unavailable", **detail})


@router.get("/metrics")
def metrics_endpoint():
    body, content_type = metrics.render()
    return Response(content=body, media_type=content_type)
