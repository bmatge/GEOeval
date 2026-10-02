"""
Middleware ASGI : identifiant de requête, journal d'accès structuré, métriques HTTP.

- lit `X-Request-ID` (posé par l'ingress) ou en génère un, le propage dans le
  contexte des logs et le renvoie dans la réponse ;
- une ligne d'accès par requête (méthode, route, statut, durée) sauf sondes ;
- compteur et histogramme Prometheus par route (gabarit, pas chemin brut).
"""
from __future__ import annotations

import logging
import time
import uuid

from geoeval.observability import metrics
from geoeval.observability.context import request_id_var

access_logger = logging.getLogger("geoeval.access")
PROBE_PATHS = {"/healthz", "/readyz", "/metrics"}


def _route_label(scope: dict) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    if not path:
        return "unmatched"
    return (scope.get("root_path") or "") + path


class RequestContextMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        request_id = (headers.get("x-request-id") or "").strip()[:128] or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_holder = {"status": 500}

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                raw = list(message.get("headers", []))
                raw.append((b"x-request-id", request_id.encode("latin-1")))
                message["headers"] = raw
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration = time.perf_counter() - started
            method = scope.get("method", "-")
            path = scope.get("path", "")
            route = _route_label(scope)
            status = status_holder["status"]
            metrics.HTTP_REQUESTS.labels(method, route, str(status)).inc()
            metrics.HTTP_DURATION.labels(method, route).observe(duration)
            if path not in PROBE_PATHS:
                access_logger.info(
                    "%s %s %s %.1fms", method, path, status, duration * 1000,
                    extra={"method": method, "path": path, "route": route, "status": status,
                           "duration_ms": round(duration * 1000, 1)},
                )
            request_id_var.reset(token)
