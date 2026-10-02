"""
Mini serveur HTTP du worker (bibliothèque standard) : /healthz, /readyz, /metrics.

Port interne GEOEVAL_WORKER_PORT (défaut 9100), jamais publié. La vivacité
repose sur le dernier signe de vie (boucle principale ou battement de cœur
d'un job en cours) : un worker figé depuis plus de STALE_SECONDS est déclaré
mort, même si le processus existe encore.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from geoeval.observability import health, metrics

logger = logging.getLogger("geoeval.worker.health")
STALE_SECONDS = 90.0


def worker_port() -> int:
    return int(os.environ.get("GEOEVAL_WORKER_PORT", "9100"))


class _Handler(BaseHTTPRequestHandler):
    server_version = "geoeval-worker"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 — signature imposée
        return  # pas de journal d'accès pour les sondes

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 — nom imposé par http.server
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            age = metrics.worker_alive_age()
            alive = age < STALE_SECONDS
            self._json(200 if alive else 503, {"status": "ok" if alive else "stale", "service": "worker",
                                                "last_alive_seconds_ago": None if age == float("inf") else round(age, 1)})
        elif path == "/readyz":
            ok, detail = health.check_db()
            self._json(200 if ok else 503, {"status": "ok" if ok else "unavailable", **detail})
        elif path == "/metrics":
            body, content_type = metrics.render()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._json(404, {"status": "not_found"})


def start_health_server(port: Optional[int] = None) -> ThreadingHTTPServer:
    """Démarre le serveur dans un thread démon ; renvoie l'instance (port réel
    dans `server.server_address[1]`, utile avec port=0 en test)."""
    server = ThreadingHTTPServer(("0.0.0.0", worker_port() if port is None else port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="worker-health").start()
    logger.info("sondes worker sur :%d (/healthz, /readyz, /metrics)", server.server_address[1])
    return server
