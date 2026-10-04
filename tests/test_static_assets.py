"""Ressources front vendorisées (lot 1.6) : aucun CDN, manifeste conforme, fichiers servis."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "geoeval" / "web" / "templates"
# Liens sortants légitimes (navigation), jamais des ressources chargées par la page.
ALLOWED_LINK_HOSTS = {"github.com"}


def test_aucune_ressource_externe_dans_les_gabarits():
    offenders = []
    for tpl in TEMPLATES.rglob("*.html"):
        for m in re.finditer(r'(href|src)="(https?://([^/"]+)[^"]*)"', tpl.read_text(encoding="utf-8")):
            attr, url, host = m.groups()
            if attr == "src" or host not in ALLOWED_LINK_HOSTS or url.endswith((".css", ".js", ".woff2")):
                offenders.append(f"{tpl.name}: {url}")
    assert offenders == [], offenders


def test_manifeste_conforme():
    from scripts.vendor_assets import verify

    assert verify() == []


def test_gabarits_pointent_sur_des_fichiers_vendorises():
    missing = []
    for tpl in (TEMPLATES / "base.html", TEMPLATES / "_dsfr_data_deps.html"):
        for m in re.finditer(r'(?:href|src)="/static/([^"]+)"', tpl.read_text(encoding="utf-8")):
            if not (ROOT / "geoeval" / "web" / "static" / m.group(1)).exists():
                missing.append(m.group(1))
    assert missing == []


@pytest.mark.integration
@pytest.mark.parametrize("path,ctype", [
    ("/static/vendor/dsfr-1.13.0/dsfr.min.css", "text/css"),
    ("/static/vendor/dsfr-1.13.0/utility/utility.min.css", "text/css"),
    ("/static/vendor/dsfr-1.13.0/dsfr.module.min.js", "javascript"),
    ("/static/vendor/dsfr-1.13.0/fonts/Marianne-Regular.woff2", "font/woff2"),
    ("/static/vendor/dsfr-1.13.0/icons/system/arrow-right-line.svg", "image/svg+xml"),
    ("/static/vendor/chartjs-4.4.1/chart.umd.js", "javascript"),
    ("/static/vendor/dsfr-chart-2.1.1/DSFRChart/DSFRChart.js", "javascript"),
    ("/static/vendor/dsfr-data-0.46.0/dsfr-data.core.umd.js", "javascript"),
])
def test_fichiers_statiques_servis(anonymous_client, path, ctype):
    r = anonymous_client.get(path)
    assert r.status_code == 200, path
    assert ctype in r.headers["content-type"], (path, r.headers["content-type"])
    assert "etag" in r.headers


@pytest.mark.integration
def test_page_rendue_sans_cdn(anonymous_client):
    html = anonymous_client.get("/").text
    assert "cdn.jsdelivr.net" not in html and "/static/vendor/dsfr-1.13.0/dsfr.min.css" in html
