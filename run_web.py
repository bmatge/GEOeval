"""
Point d'entrée de l'UI web GEOeval.

    python run_web.py            # http://127.0.0.1:8000
    python run_web.py --reload   # rechargement auto (dev)

Nécessite une base initialisée (init_db.py) et un .env (DATABASE_URL + clés API
pour lancer des runs).
"""
from __future__ import annotations

import sys

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "webapp.app:app",
        host="127.0.0.1",
        port=8000,
        reload="--reload" in sys.argv[1:],
    )
