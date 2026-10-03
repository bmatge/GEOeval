"""Hiérarchie des entités (E1) — arbre, déplacements, résolveur, liste blanche héritée, UI, API."""
from __future__ import annotations

import pytest
from sqlalchemy import select, text

from geoeval.db.models import Model, Organization
from geoeval.web import api_tokens, hierarchy as h, launching, org_models, tenancy

pytestmark = pytest.mark.integration

PREFIX = "e1-"


@pytest.fixture()
def clean(db_session):
    """Supprime les entités de test E1 (enfants d'abord) et leurs listes blanches."""
    def _purge():
        rows = db_session.execute(select(Organization).where(Organization.slug.like(PREFIX + "%")).order_by(Organization.depth.desc())).scalars().all()
        ids = [o.id for o in rows]
        if ids:
            db_session.execute(text("DELETE FROM org_models WHERE organization_id = ANY(:ids)"), {"ids": ids})
            db_session.execute(text("DELETE FROM api_tokens WHERE organization_id = ANY(:ids)"), {"ids": ids})
            db_session.execute(text("DELETE FROM audit_log WHERE org_id = ANY(:ids)"), {"ids": ids})
            db_session.execute(text("UPDATE organizations SET parent_id = NULL WHERE id = ANY(:ids)"), {"ids": ids})
            db_session.execute(text("DELETE FROM organizations WHERE id = ANY(:ids)"), {"ids": ids})
        db_session.commit()
    _purge()
    yield
    _purge()


def _mk(db, slug, parent=None, kind=None, name=None, siret=None):
    return tenancy.create_org(db, name=name or slug, slug=PREFIX + slug, parent=parent, kind=kind, siret=siret)


@pytest.fixture()
def arbre(db_session, clean):
    m = _mk(db_session, "min", kind="ministere", name="Ministère E1")
    d = _mk(db_session, "dir", parent=m, kind="direction", name="Direction E1")
    s = _mk(db_session, "svc", parent=d, kind="service", name="Service E1")
    d2 = _mk(db_session, "dir2", parent=m, kind="direction", name="Direction bis E1")
    other = _mk(db_session, "autre", name="Autre racine E1")
    return dict(m=m, d=d, s=s, d2=d2, other=other)


# ---------------------------------------------------------------------
# Arbre
# ---------------------------------------------------------------------
def test_racine_par_defaut(db_session, clean):
    o = _mk(db_session, "racine")
    assert o.parent_id is None and o.kind == "autre" and o.depth == 0 and o.path == f"/{o.id}/"


def test_creation_chemins_et_profondeurs(db_session, arbre):
    m, d, s = arbre["m"], arbre["d"], arbre["s"]
    assert (m.depth, d.depth, s.depth) == (0, 1, 2)
    assert s.path == f"/{m.id}/{d.id}/{s.id}/" and s.parent_id == d.id
    assert [o.id for o in h.lineage(db_session, s)] == [m.id, d.id]
    assert [o.id for o in h.chain(db_session, s)] == [s.id, d.id, m.id]
    assert {o.id for o in h.descendants(db_session, m)} == {d.id, s.id, arbre["d2"].id}
    assert {o.id for o in h.children(db_session, m.id)} == {arbre["d2"].id, d.id}
    assert arbre["other"].id not in h.descendant_ids(db_session, m)


def test_slug_et_nom_obligatoires_uniques(db_session, arbre):
    with pytest.raises(ValueError, match="déjà utilisé"):
        _mk(db_session, "min")
    with pytest.raises(ValueError, match="obligatoire"):
        tenancy.create_org(db_session, name="  ", slug=PREFIX + "vide")


def test_siret_et_type_valides(db_session, clean):
    o = _mk(db_session, "siret", kind="ministere", siret="110 002 011 00044")
    assert o.siret == "11000201100044"
    with pytest.raises(h.HierarchyError):
        _mk(db_session, "siret-ko", siret="123")
    with pytest.raises(h.HierarchyError):
        _mk(db_session, "kind-ko", kind="region")


def test_deplacement_reecrit_le_sous_arbre(db_session, arbre):
    m, d, s, d2 = arbre["m"], arbre["d"], arbre["s"], arbre["d2"]
    h.move(db_session, d, d2)  # la direction (et son service) passe sous la direction bis
    db_session.expire_all()
    d, s = db_session.get(Organization, d.id), db_session.get(Organization, s.id)
    assert d.parent_id == d2.id and d.depth == 2 and d.path == f"/{m.id}/{d2.id}/{d.id}/"
    assert s.depth == 3 and s.path == f"/{m.id}/{d2.id}/{d.id}/{s.id}/"
    h.move(db_session, d, None)  # puis devient une racine
    db_session.expire_all()
    d, s = db_session.get(Organization, d.id), db_session.get(Organization, s.id)
    assert d.parent_id is None and d.depth == 0 and d.path == f"/{d.id}/"
    assert s.depth == 1 and s.path == f"/{d.id}/{s.id}/"


def test_deplacement_refuse_les_cycles(db_session, arbre):
    with pytest.raises(h.HierarchyError) as ei:
        h.move(db_session, arbre["m"], arbre["s"])
    assert ei.value.status == 409
    with pytest.raises(h.HierarchyError):
        h.move(db_session, arbre["d"], arbre["d"])


def test_edition_atomique_rien_n_est_ecrit_si_le_rattachement_echoue(db_session, arbre):
    m = arbre["m"]
    with pytest.raises(h.HierarchyError):
        h.update_and_move(db_session, m, name="Nom modifié", kind="autre", move_to=arbre["s"], do_move=True)
    with pytest.raises(h.HierarchyError):
        h.update_and_move(db_session, m, name="Nom modifié", siret="123", set_siret=True, move_to=None, do_move=True)
    db_session.expire_all()
    m = db_session.get(Organization, m.id)
    assert (m.name, m.kind, m.siret, m.parent_id) == ("Ministère E1", "ministere", None, None)


def test_ui_edition_refusee_ne_modifie_rien(client, db_session, arbre):
    r = client.post(f"/admin/organizations/{arbre['m'].id}/edit", data={
        "name": "Renommé à tort", "kind": "autre", "siret": "", "parent_id": str(arbre["s"].id),
    })
    assert r.status_code == 409
    db_session.expire_all()
    assert db_session.get(Organization, arbre["m"].id).name == "Ministère E1"


def test_profondeur_maximale(db_session, clean):
    parent = _mk(db_session, "p0")
    for i in range(1, h.MAX_DEPTH + 1):
        parent = _mk(db_session, f"p{i}", parent=parent)
    assert parent.depth == h.MAX_DEPTH
    with pytest.raises(h.HierarchyError, match="Profondeur"):
        _mk(db_session, "trop-profond", parent=parent)


def test_contraintes_base(db_session, arbre):
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        db_session.execute(text("UPDATE organizations SET kind = 'region' WHERE id = :i"), {"i": arbre["m"].id})
        db_session.commit()
    db_session.rollback()
    with pytest.raises(IntegrityError):
        db_session.execute(text("UPDATE organizations SET parent_id = id WHERE id = :i"), {"i": arbre["m"].id})
        db_session.commit()
    db_session.rollback()


# ---------------------------------------------------------------------
# Résolveur
# ---------------------------------------------------------------------
def test_resolveur_nearest(db_session, arbre):
    values = {arbre["m"].id: "M", arbre["d"].id: None, arbre["s"].id: None}
    getter = lambda db, o: values.get(o.id)  # noqa: E731
    r = h.resolve_nearest(db_session, arbre["s"], getter)
    assert r.value == "M" and r.source.id == arbre["m"].id
    values[arbre["s"].id] = "S"
    assert h.resolve_nearest(db_session, arbre["s"], getter).value == "S"
    assert h.resolve_nearest(db_session, arbre["s"], getter, include_self=False).value == "M"
    assert h.resolve_nearest(db_session, arbre["other"], getter).value is None


def test_resolveur_restrictif_intersection(db_session, arbre):
    values = {arbre["m"].id: {1, 2, 3}, arbre["d"].id: {2, 3, 4}, arbre["s"].id: None}
    r = h.resolve_restrictive(db_session, arbre["s"], lambda db, o: values.get(o.id), combine=lambda a, b: a & b)
    assert r.value == {2, 3} and [o.id for o in r.sources] == [arbre["d"].id, arbre["m"].id]


# ---------------------------------------------------------------------
# Liste blanche héritée (premier consommateur du résolveur)
# ---------------------------------------------------------------------
@pytest.fixture()
def catalogue(db_session):
    models = db_session.execute(select(Model).where(Model.is_active.is_(True)).order_by(Model.model_id)).scalars().all()
    assert len(models) >= 3
    return models


def test_liste_blanche_du_parent_restreint_l_enfant(db_session, arbre, catalogue):
    a, b, c = catalogue[0], catalogue[1], catalogue[2]
    org_models.replace_allowlist(db_session, arbre["m"].id, {a.model_id, b.model_id})
    s = arbre["s"].id
    # editor du service : borné par le ministère
    assert {x.model_id for x in launching.allowed_models(db_session, s, role="editor", is_platform_admin=False)} == {a.model_id, b.model_id}
    # org_admin du service : borné aussi (il ne gère que la liste de SON entité)
    assert {x.model_id for x in launching.allowed_models(db_session, s, role="org_admin", is_platform_admin=False)} == {a.model_id, b.model_id}
    # admin plateforme : tout
    assert len(launching.allowed_models(db_session, s, role=None, is_platform_admin=True)) == len(catalogue)
    # le service ajoute c : refusé par intersection ; il retire b : appliqué
    org_models.replace_allowlist(db_session, s, {a.model_id, c.model_id})
    assert {x.model_id for x in launching.allowed_models(db_session, s, role="editor", is_platform_admin=False)} == {a.model_id}
    assert launching.allowed_model_versions(db_session, s, role="editor", is_platform_admin=False) == {a.model_version}
    # l'org_admin du service ignore sa propre liste mais pas celle du ministère
    assert {x.model_id for x in launching.allowed_models(db_session, s, role="org_admin", is_platform_admin=False)} == {a.model_id, b.model_id}
    r = org_models.resolve_allowed_ids(db_session, s)
    assert [o.id for o in r.sources] == [s, arbre["m"].id]
    # une entité hors de l'arbre n'est pas touchée
    assert launching.allowed_model_versions(db_session, arbre["other"].id, role="editor", is_platform_admin=False) is None


def test_validation_refuse_un_modele_interdit_par_le_parent(db_session, arbre, catalogue):
    a, b = catalogue[0], catalogue[1]
    org_models.replace_allowlist(db_session, arbre["m"].id, {a.model_id})
    with pytest.raises(launching.LaunchError) as ei:
        launching.validate_selection(
            db_session, arbre["d"].id, perimeter_id=0, tested_models=[b.model_version], judge_models=[a.model_version],
            repeats=1, test_ids=[1], role="org_admin", is_platform_admin=False,
        )
    assert ei.value.kind == "forbidden_models" and ei.value.extra["forbidden"] == [b.model_version]


def test_sans_liste_sur_la_chaine_comportement_inchange(db_session, arbre):
    assert org_models.effective_allowed_ids(db_session, arbre["s"].id) is None


# ---------------------------------------------------------------------
# UI admin et paramètres
# ---------------------------------------------------------------------
def test_ui_admin_creation_et_rattachement(client, db_session, arbre):
    r = client.get("/admin/organizations")
    assert r.status_code == 200 and "Ministère E1" in r.text and "Direction E1" in r.text
    r = client.post("/admin/organizations/new", data={
        "name": "Bureau E1", "slug": PREFIX + "bureau", "parent_id": str(arbre["s"].id), "kind": "service", "siret": "",
        "first_admin_email": "",
    })
    assert r.status_code == 303
    bureau = tenancy.get_org_by_slug(db_session, PREFIX + "bureau")
    assert bureau.parent_id == arbre["s"].id and bureau.depth == 3 and bureau.kind == "service"

    assert client.get(f"/admin/organizations/{bureau.id}/edit").status_code == 200
    r = client.post(f"/admin/organizations/{bureau.id}/edit", data={
        "name": "Bureau E1 renommé", "kind": "direction", "siret": "11000201100044", "parent_id": str(arbre["d2"].id),
    })
    assert r.status_code == 303
    db_session.expire_all()
    bureau = db_session.get(Organization, bureau.id)
    assert (bureau.name, bureau.kind, bureau.siret, bureau.parent_id, bureau.depth) == ("Bureau E1 renommé", "direction", "11000201100044", arbre["d2"].id, 2)

    r = client.post(f"/admin/organizations/{arbre['m'].id}/edit", data={
        "name": "Ministère E1", "kind": "ministere", "siret": "", "parent_id": str(arbre["s"].id),
    })
    assert r.status_code == 409 and "sous-arbre" in r.json()["detail"]
    r = client.post("/admin/organizations/new", data={"name": "x", "slug": PREFIX + "x", "parent_id": "", "kind": "region", "siret": "", "first_admin_email": ""})
    assert r.status_code == 400


def test_page_liste_blanche_bornee_par_le_parent(client, db_session, arbre, catalogue):
    from tests.conftest import TEST_USER_EMAIL  # noqa: F401  (admin plateforme via DEV_FAKE_EMAIL)

    a = catalogue[0]
    org_models.replace_allowlist(db_session, arbre["m"].id, {a.model_id})
    r = client.get(f"/o/{arbre['s'].slug}/settings/models")
    assert r.status_code == 200
    assert "Ministère E1" in r.text, "la source de la restriction héritée est affichée"
    assert f'value="{a.model_id}"' in r.text and f'value="{catalogue[1].model_id}"' not in r.text
    r = client.get(f"/o/{arbre['s'].slug}/settings")
    assert r.status_code == 200 and "Direction E1" in r.text  # fil d'Ariane


# ---------------------------------------------------------------------
# API v1
# ---------------------------------------------------------------------
def test_api_lecture_arbre(anonymous_client, arbre):
    r = anonymous_client.get(f"/api/v1/orgs/{arbre['s'].slug}")
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "service" and body["depth"] == 2 and body["parent_id"] == arbre["d"].id
    assert [o["slug"] for o in body["lineage"]] == [arbre["m"].slug, arbre["d"].slug]
    r = anonymous_client.get(f"/api/v1/orgs/{arbre['m'].slug}/children")
    assert {o["slug"] for o in r.json()} == {arbre["d2"].slug, arbre["d"].slug}


def test_api_ecriture_reservee_admin_plateforme(client, db_session, arbre, monkeypatch):
    """Un seul client : la session d'admin plateforme (DEV_FAKE_EMAIL) est retirée en
    cours de test pour vérifier les refus anonyme et jeton."""
    r = client.post("/api/v1/orgs", json={"name": "Cellule E1", "slug": PREFIX + "cellule", "kind": "service", "parent_slug": arbre["d"].slug})
    assert r.status_code == 201, r.text
    assert r.json()["depth"] == 2 and r.json()["parent_id"] == arbre["d"].id
    r = client.patch(f"/api/v1/orgs/{PREFIX}cellule", json={"parent_slug": None, "kind": "autre"})
    assert r.status_code == 200 and r.json()["parent_id"] is None and r.json()["depth"] == 0 and r.json()["kind"] == "autre"
    r = client.patch(f"/api/v1/orgs/{arbre['m'].slug}", json={"parent_slug": arbre["s"].slug})
    assert r.status_code == 409 and r.headers["content-type"].startswith("application/problem+json")
    assert client.post("/api/v1/orgs", json={"name": "y", "slug": PREFIX + "y", "parent_slug": "inexistante"}).status_code == 404

    _, plain = api_tokens.create(db_session, org_id=arbre["m"].id, name="t", role="org_admin", created_by=None)
    headers = {"Authorization": f"Bearer {plain}"}
    assert client.post("/api/v1/orgs", json={"name": "z", "slug": PREFIX + "z"}, headers=headers).status_code == 403, \
        "un jeton prime sur la session et n'est jamais admin plateforme"
    monkeypatch.delenv("DEV_FAKE_EMAIL", raising=False)
    assert client.post("/api/v1/orgs", json={"name": "z", "slug": PREFIX + "z"}).status_code == 401
    assert client.patch(f"/api/v1/orgs/{arbre['m'].slug}", json={"kind": "autre"}, headers=headers).status_code == 403
