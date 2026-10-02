"""
Fixtures partagées (ADR-088, lot 1).

Deux familles de tests :
- unitaires : aucune base, importent directement geoeval.core / geoeval.web ;
- intégration (marqueur `integration`) : PostgreSQL requis via DATABASE_URL.
  Le schéma est (re)créé comme le fait le service `migrate` : alembic upgrade head
  (révision 0001 convergente) → seed.sql, idempotents.

Sans DATABASE_URL, les tests d'intégration sont sautés, pas en échec.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

# geoeval/db/session.py crée l'engine à l'import (os.environ["DATABASE_URL"]). Sans base, on pose
# une URL factice pour que les modules qui importent la session (scheduler, jobs…)
# restent importables par les tests unitaires ; aucune connexion n'est ouverte.
if not DATABASE_URL:
    os.environ["DATABASE_URL"] = "postgresql+psycopg2://unit:unit@localhost:1/unit"

# Identité dev injectée par AuthMiddleware (geoeval/web/auth.py §3) — admin plateforme.
TEST_USER_EMAIL = "ci@geoeval.test"
TEST_ORG_SLUG = "ci-org"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if DATABASE_URL:
        return
    skip = pytest.mark.skip(reason="DATABASE_URL absent : tests d'intégration sautés")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


def _run_sql_file(path: Path) -> None:
    """Exécute un script SQL idempotent via psycopg2 (pas de méta-commandes psql)."""
    from geoeval.db.session import engine

    raw = engine.raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute(path.read_text(encoding="utf-8"))
        raw.commit()
    finally:
        raw.close()


@pytest.fixture(scope="session")
def db_schema():
    """Schéma par le chemin réel de déploiement (lot 1.4) : alembic upgrade head
    puis seed idempotente — exactement ce que fait `python -m scripts.migrate`."""
    from geoeval.db import migrate as m
    from geoeval.db.session import engine

    m.upgrade()
    m.apply_seed(engine)
    return engine


@pytest.fixture(scope="session")
def db_session_factory(db_schema):
    from geoeval.db.session import SessionLocal

    return SessionLocal


@pytest.fixture()
def db_session(db_session_factory):
    with db_session_factory() as session:
        yield session


@pytest.fixture(scope="session")
def test_org(db_session_factory):
    """Organisation de test, créée si absente (idempotent entre sessions)."""
    from geoeval.web import tenancy

    with db_session_factory() as session:
        org = tenancy.get_org_by_slug(session, TEST_ORG_SLUG)
        if org is None:
            org = tenancy.create_org(session, name="Org CI", slug=TEST_ORG_SLUG)
        return dict(id=org.id, slug=org.slug, name=org.name)


@pytest.fixture(scope="session")
def app(db_schema):
    """Application FastAPI, importée une seule fois (démarre worker + scheduler)."""
    os.environ.setdefault("GEOEVAL_SESSION_SECRET", "ci-secret-not-for-prod")
    os.environ["DEV_FAKE_EMAIL"] = TEST_USER_EMAIL
    os.environ["DEV_FAKE_GROUPS"] = "lab-team"
    from geoeval.web.app import app as fastapi_app

    return fastapi_app


@pytest.fixture()
def client(app, test_org):
    """Client HTTP connecté en admin plateforme via DEV_FAKE_EMAIL."""
    from fastapi.testclient import TestClient

    os.environ["DEV_FAKE_EMAIL"] = TEST_USER_EMAIL
    with TestClient(app, follow_redirects=False) as c:
        yield c


@pytest.fixture()
def anonymous_client(app, test_org, monkeypatch):
    """Client HTTP sans identité (DEV_FAKE_EMAIL retiré)."""
    from fastapi.testclient import TestClient

    monkeypatch.delenv("DEV_FAKE_EMAIL", raising=False)
    with TestClient(app, follow_redirects=False) as c:
        yield c
