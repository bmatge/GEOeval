"""Documentation de l'API v1 : schéma OpenAPI complet, Swagger UI vendorisé, export versionné (sans base)."""
from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from geoeval.web.api import MOUNT_PATH, SWAGGER_UI, TAGS, create_api_app
from scripts import export_openapi
from tests.conftest import ROOT

METHODS = ("get", "post", "put", "patch", "delete")


@pytest.fixture(scope="module")
def spec() -> dict:
    return create_api_app().openapi()


def _operations(spec: dict):
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            if method in METHODS:
                yield method.upper(), path, op


def test_chaque_route_est_documentee(spec):
    declared = {t["name"] for t in spec["tags"]}
    assert declared == {name for name, _ in TAGS} and all(t["description"] for t in spec["tags"])
    ids = []
    for method, path, op in _operations(spec):
        assert op.get("summary"), f"{method} {path} : résumé manquant"
        assert op.get("tags"), f"{method} {path} : groupe (tag) manquant"
        assert set(op["tags"]) <= declared, f"{method} {path} : groupe non déclaré dans TAGS {op['tags']}"
        ids.append(op["operationId"])
    assert len(ids) == len(set(ids)), "operationId en double"
    used = {t for _, _, op in _operations(spec) for t in op["tags"]}
    assert used == declared, f"groupes déclarés sans route : {declared - used}"


def test_schema_expose_auth_serveur_et_erreurs(spec):
    assert spec["servers"] == [{"url": MOUNT_PATH, "description": "Cette instance"}]
    assert "HTTPBearer" in spec["components"]["securitySchemes"], "bouton Authorize : jeton d'organisation"
    assert "Authorization: Bearer" in spec["info"]["description"] and "RFC 9457" in spec["info"]["description"]
    _, _, op = next(o for o in _operations(spec) if o[1] == "/orgs/{org_slug}/perimeters" and o[0] == "GET")
    for status in ("401", "403", "404", "422"):
        assert "application/problem+json" in op["responses"][status]["content"], f"erreur {status} en problem+json"


def test_swagger_ui_servi_sans_cdn():
    client = TestClient(create_api_app())
    page = client.get("/docs")
    assert page.status_code == 200 and "swagger-ui" in page.text
    urls = re.findall(r"""(?:src|href)=["']([^"']+)""", page.text) + re.findall(r"""url:\s*['"]([^'"]+)""", page.text)
    assert urls and all(u.startswith("/") for u in urls), f"ressource externe dans la page de documentation : {urls}"
    assert not re.search(r"https?://(?!www\.w3\.org)", page.text), "aucune URL externe (CDN) dans la page"
    for name in ("swagger-ui-bundle.js", "swagger-ui.css", "favicon-32x32.png"):
        assert f"{SWAGGER_UI}/{name}" in page.text
        assert (ROOT / "geoeval" / "web" / SWAGGER_UI.lstrip("/") / name).is_file(), f"{name} non vendorisé"
    assert f"{MOUNT_PATH}/openapi.json" in page.text
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 200


def test_export_versionne_a_jour():
    """docs/openapi.json est le contrat relu en PR : il doit suivre le code (`python -m scripts.export_openapi`)."""
    target = ROOT / "docs" / "openapi.json"
    assert target.is_file(), "docs/openapi.json absent : lance `python -m scripts.export_openapi`"
    committed = export_openapi.contract(json.loads(target.read_text(encoding="utf-8")))
    current = export_openapi.contract(json.loads(export_openapi.render()))
    drift = sorted(k for k in set(committed["operations"]) | set(current["operations"])
                   if committed["operations"].get(k) != current["operations"].get(k))
    assert not drift, f"routes modifiées sans régénérer docs/openapi.json (`python -m scripts.export_openapi`) : {drift}"
    assert committed == current, "docs/openapi.json n'est plus à jour : lance `python -m scripts.export_openapi`"
