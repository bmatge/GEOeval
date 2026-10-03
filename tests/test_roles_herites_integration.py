"""Rôles hérités vers le bas et délégation de structure (E2) — accès UI et API,
jetons hérités, liste blanche d'un org_admin hérité, membres hérités."""
from __future__ import annotations

import pytest
from sqlalchemy import select, text

from geoeval.db.models import Model, Organization
from geoeval.web import api_tokens, launching, org_models, tenancy

pytestmark = pytest.mark.integration

PREFIX = "e2-"


@pytest.fixture()
def clean(db_session):
    def _purge():
        rows = db_session.execute(select(Organization).where(Organization.slug.like(PREFIX + "%"))).scalars().all()
        ids = [o.id for o in rows]
        if ids:
            for tbl, col in (("org_models", "organization_id"), ("api_tokens", "organization_id"),
                             ("memberships", "org_id"), ("invitations", "org_id"), ("audit_log", "org_id")):
                db_session.execute(text(f"DELETE FROM {tbl} WHERE {col} = ANY(:ids)"), {"ids": ids})
            db_session.execute(text("UPDATE organizations SET parent_id = NULL WHERE id = ANY(:ids)"), {"ids": ids})
            db_session.execute(text("DELETE FROM organizations WHERE id = ANY(:ids)"), {"ids": ids})
        db_session.commit()
    _purge()
    yield
    _purge()


def _mk(db, slug, parent=None, kind=None, name=None):
    return tenancy.create_org(db, name=name or f"{slug.upper()} E2", slug=PREFIX + slug, parent=parent, kind=kind)


@pytest.fixture()
def arbre(db_session, clean):
    m = _mk(db_session, "min", kind="ministere", name="Ministère E2")
    d = _mk(db_session, "dir", parent=m, kind="direction", name="Direction E2")
    s = _mk(db_session, "svc", parent=d, kind="service", name="Service E2")
    d2 = _mk(db_session, "dir2", parent=m, kind="direction", name="Direction bis E2")
    x = _mk(db_session, "ailleurs", name="Ailleurs E2")
    return dict(m=m, d=d, s=s, d2=d2, x=x)


@pytest.fixture()
def as_user(db_session, monkeypatch):
    """Bascule l'identité DEV_FAKE_EMAIL sur un utilisateur NON admin plateforme
    et lui pose des adhésions."""
    from geoeval.web.auth import load_or_provision_user

    def _login(email: str, memberships: dict):
        cu = load_or_provision_user(db_session, email, groups=[])
        db_session.execute(text("DELETE FROM memberships WHERE user_id = :u"), {"u": cu.id})
        db_session.commit()
        for org, role in memberships.items():
            tenancy.add_membership(db_session, user_id=cu.id, org_id=org.id, role=role)
        monkeypatch.setenv("DEV_FAKE_EMAIL", email)
        monkeypatch.setenv("DEV_FAKE_GROUPS", "")
        return cu
    return _login


# ---------------------------------------------------------------------
# Accès UI par héritage
# ---------------------------------------------------------------------
def test_viewer_du_ministere_voit_tout_le_sous_arbre(client, arbre, as_user):
    as_user("viewer-min@e2.test", {arbre["m"]: "viewer"})
    for key in ("m", "d", "s", "d2"):
        assert client.get(f"/o/{arbre[key].slug}/tests").status_code == 200, key
    assert client.get(f"/o/{arbre['x'].slug}/tests").status_code == 404, "hors arbre : invisible"
    assert client.get(f"/o/{arbre['s'].slug}/launch").status_code == 403, "viewer : lecture seule"
    r = client.get("/")
    assert r.status_code == 200 and "Service E2" in r.text and "Ailleurs E2" not in r.text


def test_role_pose_sur_un_service_ne_remonte_pas(client, arbre, as_user):
    as_user("editor-svc@e2.test", {arbre["s"]: "editor"})
    assert client.get(f"/o/{arbre['s'].slug}/launch").status_code == 200
    assert client.get(f"/o/{arbre['d'].slug}/tests").status_code == 404
    assert client.get(f"/o/{arbre['m'].slug}/tests").status_code == 404
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == f"/o/{arbre['s'].slug}/", "une seule entité accessible"


def test_role_effectif_maximum(client, arbre, as_user):
    as_user("mix@e2.test", {arbre["m"]: "viewer", arbre["d"]: "editor"})
    assert client.get(f"/o/{arbre['s'].slug}/launch").status_code == 200, "editor hérité de la direction"
    assert client.get(f"/o/{arbre['d2'].slug}/launch").status_code == 403, "viewer seulement sur la branche sœur"


def test_org_admin_herite_gere_les_membres_d_une_sous_entite(client, db_session, arbre, as_user):
    as_user("admin-min@e2.test", {arbre["m"]: "org_admin"})
    r = client.post(f"/o/{arbre['s'].slug}/invitations", data={"email": "nouveau@e2.test", "role": "editor"})
    assert r.status_code == 303
    assert tenancy.list_invitations(db_session, arbre["s"].id)[0].email == "nouveau@e2.test"


def test_membres_herites_affiches(client, db_session, arbre, as_user):
    as_user("admin-min2@e2.test", {arbre["m"]: "org_admin"})
    r = client.get(f"/o/{arbre['s'].slug}/settings")
    assert r.status_code == 200
    assert "Membres hérités" in r.text and "admin-min2@e2.test" in r.text and "Ministère E2" in r.text
    inherited = tenancy.list_inherited_members(db_session, arbre["s"])
    assert any(m["email"] == "admin-min2@e2.test" and m["source_id"] == arbre["m"].id for m in inherited)


# ---------------------------------------------------------------------
# Délégation de structure (UI)
# ---------------------------------------------------------------------
def test_org_admin_cree_et_deplace_dans_son_sous_arbre(client, db_session, arbre, as_user):
    as_user("deleg@e2.test", {arbre["m"]: "org_admin"})
    r = client.get(f"/o/{arbre['m'].slug}/settings/entities")
    assert r.status_code == 200 and "Service E2" in r.text and "Ailleurs E2" not in r.text
    r = client.post(f"/o/{arbre['m'].slug}/settings/entities", data={
        "name": "Bureau E2", "slug": PREFIX + "bureau", "parent_id": str(arbre["s"].id), "kind": "service", "siret": ""})
    assert r.status_code == 303
    bureau = tenancy.get_org_by_slug(db_session, PREFIX + "bureau")
    assert bureau.parent_id == arbre["s"].id and bureau.depth == 3

    r = client.post(f"/o/{arbre['m'].slug}/settings/entities/{arbre['s'].id}/edit", data={
        "name": "Service E2", "kind": "service", "siret": "", "parent_id": str(arbre["d2"].id)})
    assert r.status_code == 303
    db_session.expire_all()
    s = db_session.get(Organization, arbre["s"].id)
    assert s.parent_id == arbre["d2"].id and db_session.get(Organization, bureau.id).path.startswith(s.path)


def test_delegation_refusee_hors_perimetre(client, db_session, arbre, as_user):
    as_user("deleg-dir@e2.test", {arbre["d"]: "org_admin"})
    base = f"/o/{arbre['d'].slug}/settings/entities"
    # parent hors du périmètre de la page
    r = client.post(base, data={"name": "x", "slug": PREFIX + "x", "parent_id": str(arbre["d2"].id), "kind": "autre", "siret": ""})
    assert r.status_code == 400
    # l'entité de rattachement elle-même n'est pas éditable par délégation
    assert client.get(f"{base}/{arbre['d'].id}/edit").status_code == 404
    # cible hors périmètre : invisible
    assert client.get(f"{base}/{arbre['d2'].id}/edit").status_code == 404
    # déplacer le service sous la direction bis (hors sous-arbre de D) : refusé
    r = client.post(f"{base}/{arbre['s'].id}/edit", data={"name": "Service E2", "kind": "service", "siret": "", "parent_id": str(arbre["d2"].id)})
    assert r.status_code == 400
    # l'administration plateforme reste inaccessible
    assert client.get("/admin/organizations").status_code == 403
    # un editor n'accède pas à la page
    as_user("editor-dir@e2.test", {arbre["d"]: "editor"})
    assert client.get(base).status_code == 403


# ---------------------------------------------------------------------
# Liste blanche d'un org_admin hérité
# ---------------------------------------------------------------------
def test_liste_blanche_bornee_au_dessus_de_l_ancre(db_session, arbre):
    models = db_session.execute(select(Model).where(Model.is_active.is_(True)).order_by(Model.model_id)).scalars().all()
    a, b = models[0], models[1]
    org_models.replace_allowlist(db_session, arbre["d"].id, {a.model_id})
    s = arbre["s"]
    editor = tenancy.resolve_role({arbre["m"].id: "editor"}, s)
    admin_min = tenancy.resolve_role({arbre["m"].id: "org_admin"}, s)
    admin_svc = tenancy.resolve_role({s.id: "org_admin"}, s)
    ids = lambda role: {x.model_id for x in launching.allowed_models(db_session, s.id, role=role, is_platform_admin=False)}  # noqa: E731
    assert ids(editor) == {a.model_id}, "editor : borné par la liste de la direction"
    assert b.model_id in ids(admin_min), "admin du ministère : gère la liste de la direction, pas borné par elle"
    assert ids(admin_svc) == {a.model_id}, "admin du service : borné par la direction au-dessus de lui"
    # Une liste posée sur sa propre ancre ne borne pas l'admin du ministère (il la gère),
    # mais borne l'admin du service, qui est en dessous.
    org_models.replace_allowlist(db_session, arbre["m"].id, {a.model_id})
    assert b.model_id in ids(admin_min)
    assert launching.allowed_model_versions(db_session, s.id, role=admin_min, is_platform_admin=False) is None
    assert ids(admin_svc) == {a.model_id}
    # Un admin du ministère reste borné par une liste posée AU-DESSUS de lui.
    top = _mk(db_session, "top", kind="autre", name="Sommet E2")
    from geoeval.web import hierarchy
    hierarchy.move(db_session, arbre["m"], top)
    org_models.replace_allowlist(db_session, top.id, {a.model_id})
    db_session.expire_all()
    s = db_session.get(Organization, s.id)
    admin_min = tenancy.resolve_role({arbre["m"].id: "org_admin"}, s)
    assert admin_min.anchor_depth == 1
    assert {x.model_id for x in launching.allowed_models(db_session, s.id, role=admin_min, is_platform_admin=False)} == {a.model_id}


# ---------------------------------------------------------------------
# API : jetons hérités et délégation
# ---------------------------------------------------------------------
def _bearer(db, org, role):
    _, plain = api_tokens.create(db, org_id=org.id, name=f"tok-{role}", role=role, created_by=None)
    return {"Authorization": f"Bearer {plain}"}


def test_jeton_herite_sur_le_sous_arbre(anonymous_client, db_session, arbre):
    h = _bearer(db_session, arbre["m"], "editor")
    for key in ("m", "d", "s"):
        assert anonymous_client.get(f"/api/v1/orgs/{arbre[key].slug}/questions", headers=h).status_code == 200, key
    assert anonymous_client.get(f"/api/v1/orgs/{arbre['x'].slug}/questions", headers=h).status_code == 404
    slugs = {o["slug"] for o in anonymous_client.get("/api/v1/orgs", headers=h).json()}
    assert slugs == {arbre[k].slug for k in ("m", "d", "s", "d2")}
    h_svc = _bearer(db_session, arbre["s"], "org_admin")
    assert anonymous_client.get(f"/api/v1/orgs/{arbre['d'].slug}/questions", headers=h_svc).status_code == 404, "jamais vers le haut"


def test_api_structure_deleguee(anonymous_client, db_session, arbre):
    h = _bearer(db_session, arbre["m"], "org_admin")
    r = anonymous_client.post("/api/v1/orgs", json={"name": "Cellule E2", "slug": PREFIX + "cellule", "parent_slug": arbre["d"].slug, "kind": "service"}, headers=h)
    assert r.status_code == 201, r.text
    r = anonymous_client.patch(f"/api/v1/orgs/{PREFIX}cellule", json={"parent_slug": arbre["d2"].slug}, headers=h)
    assert r.status_code == 200 and r.json()["parent_id"] == arbre["d2"].id
    assert anonymous_client.patch(f"/api/v1/orgs/{PREFIX}cellule", json={"parent_slug": None}, headers=h).status_code == 403
    assert anonymous_client.patch(f"/api/v1/orgs/{PREFIX}cellule", json={"parent_slug": arbre["x"].slug}, headers=h).status_code == 403
    assert anonymous_client.patch(f"/api/v1/orgs/{arbre['m'].slug}", json={"kind": "autre"}, headers=h).status_code == 403
    assert anonymous_client.post("/api/v1/orgs", json={"name": "r", "slug": PREFIX + "r"}, headers=h).status_code == 403
    h_ed = _bearer(db_session, arbre["m"], "editor")
    assert anonymous_client.post("/api/v1/orgs", json={"name": "e", "slug": PREFIX + "e", "parent_slug": arbre["d"].slug}, headers=h_ed).status_code == 403
