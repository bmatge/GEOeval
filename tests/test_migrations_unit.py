"""Migrations Alembic (lot 1.4) — vérifications statiques, sans base."""
from __future__ import annotations

import re
from pathlib import Path

from geoeval.db import migrate as m

ROOT = Path(__file__).resolve().parent.parent


def test_une_seule_tete_et_chaine_lineaire():
    from alembic.script import ScriptDirectory

    cfg = m.alembic_config("postgresql+psycopg2://x:y@localhost/z")
    script = ScriptDirectory.from_config(cfg)
    assert len(script.get_heads()) == 1
    revs = list(script.walk_revisions())  # de la tête vers la base
    assert revs[-1].revision == "0001" and revs[-1].down_revision is None
    for newer, older in zip(revs, revs[1:]):
        assert newer.down_revision == older.revision, "chaîne de révisions linéaire"


def test_instantane_sans_meta_commandes_psql():
    text = (ROOT / "geoeval/db/alembic/schema_base.sql").read_text()
    for line in text.splitlines():
        assert not line.startswith("\\"), line
        assert not re.match(r"^(SET |SELECT pg_catalog\.set_config)", line), line
    assert text.count("CREATE TABLE") == 25
    assert "OWNER TO" not in text


def test_fichiers_references_par_la_revision_existent():
    import importlib.util

    spec = importlib.util.spec_from_file_location("rev0001", ROOT / "geoeval/db/alembic/versions/0001_base_convergente.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.SCHEMA_BASE_SQL.exists() and mod.LEGACY_MIGRATIONS_SQL.exists()
    assert mod.revision == "0001" and mod.down_revision is None


def test_migrations_sql_est_gele():
    head = (ROOT / "geoeval/db/migrations.sql").read_text().splitlines()[0:6]
    assert any("GEL" in line.upper() for line in head), "migrations.sql doit annoncer son gel (lot 1.4)"
