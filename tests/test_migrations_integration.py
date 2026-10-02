"""Migrations Alembic (lot 1.4) — chemin réel, dérive, idempotence, convergence."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

from geoeval.db import migrate as m

pytestmark = pytest.mark.integration


def test_base_de_test_a_la_tete(db_schema):
    assert m.current_revision(db_schema) == m.head_revision() == "0001"


def test_aucune_derive_orm_base(db_schema):
    drift = m.schema_drift(db_schema)
    assert drift == [], f"models.py et la base migrée divergent : {drift}"


def test_upgrade_rejoue_sans_effet(db_schema):
    m.upgrade()
    assert m.current_revision(db_schema) == "0001"


def test_script_check_retourne_0(db_schema):
    from scripts.migrate import main

    assert main(["--check"]) == 0


def _describe(engine) -> dict:
    insp = inspect(engine)
    out = {}
    for t in sorted(insp.get_table_names()):
        if t == "alembic_version":
            continue
        cols = {c["name"]: str(c["type"]) for c in insp.get_columns(t)}
        idx = sorted(i["name"] for i in insp.get_indexes(t))
        uniq = sorted(u["name"] for u in insp.get_unique_constraints(t) if u.get("name"))
        fks = sorted((fk["referred_table"], tuple(fk["constrained_columns"])) for fk in insp.get_foreign_keys(t))
        out[t] = dict(cols=cols, idx=idx, uniq=uniq, fks=fks)
    return out


def test_convergence_base_vierge_vs_base_existante(db_schema):
    """La révision 0001 produit le même schéma qu'elle parte d'une base vierge
    (schema_base.sql) ou d'une base existante (migrations.sql)."""
    url = m.database_url()
    admin_url = url.rsplit("/", 1)[0] + "/postgres"
    scratch = "geoeval_scratch_convergence"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {scratch}"))
        conn.execute(text(f"CREATE DATABASE {scratch}"))
    scratch_url = url.rsplit("/", 1)[0] + f"/{scratch}"
    try:
        m.upgrade(scratch_url)
        fresh = create_engine(scratch_url)
        assert m.current_revision(fresh) == "0001"
        assert m.schema_drift(fresh) == []
        a, b = _describe(fresh), _describe(db_schema)
        fresh.dispose()
        assert a.keys() == b.keys()
        for t in a:
            assert a[t]["cols"] == b[t]["cols"], f"colonnes de {t}"
            assert a[t]["idx"] == b[t]["idx"], f"index de {t}"
            assert a[t]["fks"] == b[t]["fks"], f"clés étrangères de {t}"
    finally:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {scratch} WITH (FORCE)"))
        admin.dispose()
