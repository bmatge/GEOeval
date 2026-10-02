"""Tests d'intégration : schéma PostgreSQL + pages principales (TestClient).

Reproduit le chemin de deploy/docker-entrypoint.sh (create_all → migrations → seed) puis
vérifie que les pages touchées par chaque PR répondent. Marqueur `integration` :
sauté sans DATABASE_URL.
"""
from __future__ import annotations

import pytest
from sqlalchemy import inspect, text

from tests.conftest import TEST_ORG_SLUG

pytestmark = pytest.mark.integration


def test_schema_complet_apres_migrations(db_schema):
    from geoeval.db.models import Base

    tables = set(inspect(db_schema).get_table_names())
    missing = set(Base.metadata.tables) - tables
    assert not missing, f"tables ORM absentes en base : {sorted(missing)}"


def test_migrations_et_seed_idempotentes(db_schema):
    """Rejouer migrations.sql + seed.sql ne doit rien casser (contrat du conteneur)."""
    from tests.conftest import ROOT, _run_sql_file

    _run_sql_file(ROOT / "geoeval" / "db" / "migrations.sql")
    _run_sql_file(ROOT / "geoeval" / "db" / "seed.sql")


def test_seed_catalogue_modeles(db_session):
    n = db_session.execute(text("SELECT count(*) FROM models")).scalar_one()
    assert n >= 3
    judges = db_session.execute(text("SELECT count(*) FROM models WHERE is_judge")).scalar_one()
    assert judges >= 1


def test_accueil_redirige_vers_unique_org(client):
    r = client.get("/")
    assert r.status_code in (200, 302)
    if r.status_code == 302:
        assert r.headers["location"].startswith("/o/")


@pytest.mark.parametrize(
    "path",
    ["/", "/dashboard", "/runs", "/tests", "/models", "/launch", "/schedules", "/jobs", "/perimeters", "/prompts", "/settings"],
)
def test_pages_org_repondent(client, path):
    r = client.get(f"/o/{TEST_ORG_SLUG}{path}")
    assert r.status_code == 200, f"{path} → {r.status_code}"
    assert "text/html" in r.headers["content-type"]


def test_api_stats_json(client):
    r = client.get(f"/o/{TEST_ORG_SLUG}/api/stats/summary")
    assert r.status_code == 200
    assert "application/json" in r.headers["content-type"]


def test_org_inconnue_404(client):
    assert client.get("/o/org-inexistante/dashboard").status_code == 404


def test_admin_accessible_a_l_admin_plateforme(client):
    assert client.get("/admin/organizations").status_code == 200
    assert client.get("/admin/users").status_code == 200


def test_admin_refuse_aux_anonymes(anonymous_client):
    r = anonymous_client.get("/admin/organizations")
    assert r.status_code in (302, 401, 403), r.status_code


def test_dashboard_public_en_lecture(anonymous_client):
    """ADR-087 : tableau de bord consultable sans compte."""
    assert anonymous_client.get(f"/o/{TEST_ORG_SLUG}/dashboard").status_code == 200


def test_lancement_refuse_aux_anonymes(anonymous_client):
    r = anonymous_client.get(f"/o/{TEST_ORG_SLUG}/launch")
    assert r.status_code in (302, 401, 403), r.status_code
