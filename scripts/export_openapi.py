"""
Export du schéma OpenAPI de l'API v1 vers docs/openapi.json (ADR-088 §2.3 — API first).

Le schéma versionné sert de contrat lisible hors de l'application (revue de PR, génération
de clients, import dans un outil d'API). Aucune base n'est nécessaire.

    python -m scripts.export_openapi            # (ré)écrit docs/openapi.json
    python -m scripts.export_openapi --check    # échoue si le fichier n'est plus à jour (CI)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "docs" / "openapi.json"


def render() -> str:
    # geoeval/db/session.py crée l'engine à l'import : une URL factice suffit, aucune connexion n'est ouverte.
    os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://export:export@localhost:1/export")
    from geoeval.web.api import create_api_app

    return json.dumps(create_api_app().openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def contract(spec: dict) -> dict:
    """Le contrat à comparer : routes, résumés, groupes, paramètres, schémas nommés et leurs champs.
    Volontairement indépendant du détail de génération (qui varie d'une version de FastAPI à l'autre)."""
    ops = {}
    for path, item in spec.get("paths", {}).items():
        for method, op in item.items():
            if method in ("get", "post", "put", "patch", "delete"):
                ops[f"{method.upper()} {path}"] = {
                    "summary": op.get("summary"), "tags": op.get("tags"),
                    "parameters": sorted(f"{p.get('in')}:{p.get('name')}" for p in op.get("parameters", [])),
                    "body": bool(op.get("requestBody")),
                    "success": sorted(c for c in op.get("responses", {}) if c.startswith("2")),
                }
    schemas = {name: sorted((sch.get("properties") or {}).keys())
               for name, sch in spec.get("components", {}).get("schemas", {}).items()}
    info = {k: spec.get("info", {}).get(k) for k in ("title", "version", "summary", "description")}
    return {"info": info, "tags": spec.get("tags"), "servers": spec.get("servers"), "operations": ops, "schemas": schemas}


def is_up_to_date() -> bool:
    if not TARGET.exists():
        return False
    return contract(json.loads(TARGET.read_text(encoding="utf-8"))) == contract(json.loads(render()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--check", action="store_true", help="vérifie que docs/openapi.json est à jour")
    args = parser.parse_args(argv)
    content = render()
    if args.check:
        if not is_up_to_date():
            print("✗ docs/openapi.json n'est plus à jour : lance `python -m scripts.export_openapi`.", file=sys.stderr)
            return 1
        print("✓ docs/openapi.json à jour")
        return 0
    TARGET.write_text(content, encoding="utf-8")
    spec = json.loads(content)
    n_ops = sum(1 for ops in spec["paths"].values() for m in ops if m in ("get", "post", "put", "patch", "delete"))
    print(f"docs/openapi.json écrit : {len(spec['paths'])} chemins, {n_ops} opérations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
