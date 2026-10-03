"""
Résolution d'organisation par slug + helpers de convenance.

Séparé de `services.py` pour ne pas mélanger la couche « auth/tenancy » (qui
lit des IDs) avec les DAO du domaine benchmark (qui filtrent par org_id).
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from geoeval.db.models import Invitation, Membership, Organization, User


# Rôles reconnus au sein d'une organisation (voir ADR-077 §3).
ROLES = ("org_admin", "editor", "viewer")

# Poids par rôle pour la comparaison "au moins ce niveau".
_ROLE_WEIGHT = {"viewer": 0, "editor": 1, "org_admin": 2}

INVITATION_TTL = timedelta(days=30)


def get_org_by_slug(session: Session, slug: str) -> Optional[Organization]:
    return session.execute(
        select(Organization).where(Organization.slug == slug)
    ).scalar_one_or_none()


def get_org(session: Session, org_id: int) -> Optional[Organization]:
    return session.get(Organization, org_id)


def list_orgs_for_user(session: Session, user_id: int) -> list[Organization]:
    rows = session.execute(
        select(Organization)
        .join(Membership, Membership.org_id == Organization.id)
        .where(Membership.user_id == user_id)
        .order_by(Organization.name)
    ).scalars().all()
    return list(rows)


def list_all_orgs(session: Session) -> list[Organization]:
    return list(
        session.execute(select(Organization).order_by(Organization.name)).scalars().all()
    )


def create_org(
    session: Session,
    *,
    name: str,
    slug: str,
    created_by: Optional[int] = None,
    parent: Optional[Organization] = None,
    kind: Optional[str] = None,
    siret: Optional[str] = None,
) -> Organization:
    """Crée une entité, racine par défaut (type « autre »), ou rattachée à `parent`
    (ADR-089 §2.1 : chemin et profondeur calculés par geoeval.web.hierarchy)."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9\-]{1,63}", slug):
        raise ValueError(
            f"slug invalide {slug!r}: minuscules, chiffres et tirets uniquement (2–64 chars)."
        )
    if not (name or "").strip():
        raise ValueError("Le nom de l'entité est obligatoire.")
    if get_org_by_slug(session, slug.strip()) is not None:
        raise ValueError(f"le slug {slug!r} est déjà utilisé.")
    from geoeval.web import hierarchy  # import local : hierarchy dépend de models seulement

    org = hierarchy.new_organization(
        session, name=name, slug=slug, parent=parent, kind=kind, siret=siret, created_by=created_by,
    )
    session.commit()
    return org


def rename_org(session: Session, org_id: int, *, name: str) -> Organization:
    org = session.get(Organization, org_id)
    if org is None:
        raise ValueError(f"organization {org_id} introuvable")
    org.name = name.strip()
    session.commit()
    return org


def add_membership(
    session: Session,
    *,
    user_id: int,
    org_id: int,
    role: str,
) -> Membership:
    if role not in ROLES:
        raise ValueError(f"role invalide: {role!r} (attendu: {', '.join(ROLES)})")
    m = Membership(user_id=user_id, org_id=org_id, role=role)
    session.add(m)
    session.commit()
    return m


def set_membership_role(
    session: Session, *, user_id: int, org_id: int, role: str
) -> Membership:
    if role not in ROLES:
        raise ValueError(f"role invalide: {role!r}")
    m = session.get(Membership, (user_id, org_id))
    if m is None:
        raise ValueError(f"pas de membership user={user_id} / org={org_id}")
    m.role = role
    session.commit()
    return m


def remove_membership(session: Session, *, user_id: int, org_id: int) -> None:
    m = session.get(Membership, (user_id, org_id))
    if m is not None:
        session.delete(m)
        session.commit()


def list_members(session: Session, org_id: int) -> list[dict[str, Any]]:
    """Renvoie les membres d'une org avec leur email et rôle."""
    rows = session.execute(
        select(User.id, User.email, Membership.role, Membership.created_at)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.org_id == org_id)
        .order_by(User.email)
    ).all()
    return [
        dict(user_id=r.id, email=r.email, role=r.role, created_at=r.created_at)
        for r in rows
    ]


def role_at_least(actual: Optional[str], required: str) -> bool:
    if actual is None:
        return False
    return _ROLE_WEIGHT.get(actual, -1) >= _ROLE_WEIGHT[required]


# =====================================================================
# Rôles hérités vers le bas (ADR-089 §2.4, chantier E2)
# =====================================================================
class EffectiveRole(str):
    """Rôle effectif sur une entité : une chaîne (« viewer », « editor »,
    « org_admin ») qui connaît l'entité qui le porte.

    Sous-classe de `str` : tout le code existant qui compare ou affiche le rôle
    fonctionne tel quel. `anchor_id` / `anchor_depth` désignent l'entité ancêtre
    (ou l'entité elle-même) où le rôle retenu est posé ; ils servent à borner la
    liste blanche d'un org_admin hérité (il gère les listes de son sous-arbre,
    pas celles des entités au-dessus de son ancre).
    """

    anchor_id: Optional[int]
    anchor_depth: Optional[int]

    def __new__(cls, role: str, anchor_id: Optional[int] = None, anchor_depth: Optional[int] = None):
        obj = super().__new__(cls, role)
        obj.anchor_id = anchor_id
        obj.anchor_depth = anchor_depth
        return obj


def resolve_role(anchors: dict[int, str], org: Organization) -> Optional[EffectiveRole]:
    """Rôle effectif = maximum des rôles posés sur l'entité et ses ancêtres.

    `anchors` : {org_id: rôle} (adhésions d'un utilisateur, ou {org du jeton: rôle}).
    À rôle égal, l'ancre la plus haute est retenue (elle couvre le plus large).
    """
    from geoeval.web.hierarchy import ids_from_path

    best: Optional[EffectiveRole] = None
    for depth, node_id in enumerate(ids_from_path(org.path)):
        role = anchors.get(node_id)
        if role is None or role not in _ROLE_WEIGHT:
            continue
        if best is None or _ROLE_WEIGHT[role] > _ROLE_WEIGHT[str(best)]:
            best = EffectiveRole(role, anchor_id=node_id, anchor_depth=depth)
    return best


def effective_role_for_user(user, org: Organization) -> Optional[EffectiveRole]:
    """Rôle effectif d'un CurrentUser ; l'admin plateforme vaut org_admin implicite
    ancré à la racine de l'arbre."""
    role = resolve_role(user.memberships, org)
    if role is None and user.is_platform_admin:
        from geoeval.web.hierarchy import ids_from_path

        return EffectiveRole("org_admin", anchor_id=ids_from_path(org.path)[0], anchor_depth=0)
    return role


# ---- Délégation de la structure (arbitrage E2) -----------------------
def _admin_anchor_ids(anchors: dict[int, str]) -> set[int]:
    return {oid for oid, role in anchors.items() if role == "org_admin"}


def can_create_under(anchors: dict[int, str], is_platform_admin: bool, parent: Optional[Organization]) -> bool:
    """Créer une sous-entité sous `parent` : admin plateforme, ou org_admin effectif
    sur `parent` (ancre = parent ou un de ses ancêtres). Créer une racine : admin
    plateforme seulement."""
    if is_platform_admin:
        return True
    if parent is None:
        return False
    from geoeval.web.hierarchy import ids_from_path

    return bool(_admin_anchor_ids(anchors) & set(ids_from_path(parent.path)))


def can_qualify(anchors: dict[int, str], is_platform_admin: bool, org: Organization) -> bool:
    """Qualifier (nom, type, SIRET) : admin plateforme, ou org_admin d'un ancêtre
    STRICT. L'entité de rattachement elle-même n'est requalifiée que par la
    plateforme (le renommage reste possible depuis ses paramètres)."""
    if is_platform_admin:
        return True
    from geoeval.web.hierarchy import ancestor_ids

    return bool(_admin_anchor_ids(anchors) & set(ancestor_ids(org)))


def can_restructure(
    anchors: dict[int, str], is_platform_admin: bool, org: Organization, new_parent: Optional[Organization]
) -> bool:
    """Déplacer `org` sous `new_parent` : admin plateforme ; sinon il faut une
    ancre org_admin qui soit ancêtre STRICT de `org` ET ancêtre-ou-soi du nouveau
    parent — on ne sort jamais une entité de son périmètre, on n'en fait jamais
    une racine, on ne déplace pas son entité de rattachement."""
    if is_platform_admin:
        return True
    if new_parent is None:
        return False
    from geoeval.web.hierarchy import ancestor_ids, ids_from_path

    admin = _admin_anchor_ids(anchors)
    return bool(admin & set(ancestor_ids(org)) & set(ids_from_path(new_parent.path)))


def list_accessible_orgs(session: Session, user_id: int) -> list[Organization]:
    """Entités où l'utilisateur a un rôle effectif : ses adhésions et tout leur
    sous-arbre (rôles hérités vers le bas)."""
    from sqlalchemy import or_

    roots = list_orgs_for_user(session, user_id)
    if not roots:
        return []
    rows = session.execute(
        select(Organization).where(or_(*[Organization.path.like(r.path + "%") for r in roots]))
    ).scalars().all()
    from geoeval.web.hierarchy import tree

    return tree(rows)


def list_inherited_members(session: Session, org: Organization) -> list[dict[str, Any]]:
    """Membres posés sur les entités ancêtres (rôle hérité, lecture seule ici)."""
    from geoeval.web.hierarchy import ancestor_ids

    ids = ancestor_ids(org)
    if not ids:
        return []
    rows = session.execute(
        select(User.id, User.email, Membership.role, Organization.id, Organization.name, Organization.depth)
        .join(Membership, Membership.user_id == User.id)
        .join(Organization, Organization.id == Membership.org_id)
        .where(Membership.org_id.in_(ids))
        .order_by(Organization.depth, User.email)
    ).all()
    return [
        dict(user_id=r[0], email=r[1], role=r[2], source_id=r[3], source_name=r[4])
        for r in rows
    ]


# =====================================================================
# Invitations (ADR-077 §5)
# =====================================================================
def _new_token() -> str:
    return secrets.token_urlsafe(32)


def create_invitation(
    session: Session,
    *,
    org_id: int,
    email: str,
    role: str,
    invited_by: int,
) -> Invitation:
    if role not in ROLES:
        raise ValueError(f"role invalide: {role!r}")
    inv = Invitation(
        org_id=org_id,
        email=email.strip().lower(),
        role=role,
        invited_by=invited_by,
        token=_new_token(),
    )
    session.add(inv)
    session.commit()
    return inv


def list_invitations(session: Session, org_id: int) -> list[Invitation]:
    return list(
        session.execute(
            select(Invitation)
            .where(Invitation.org_id == org_id)
            .order_by(Invitation.created_at.desc())
        ).scalars().all()
    )


def get_invitation_by_token(session: Session, token: str) -> Optional[Invitation]:
    return session.execute(
        select(Invitation).where(Invitation.token == token)
    ).scalar_one_or_none()


def revoke_invitation(session: Session, org_id: int, inv_id: int) -> None:
    inv = session.get(Invitation, inv_id)
    if inv is None or inv.org_id != org_id:
        raise ValueError(f"invitation {inv_id} introuvable pour org {org_id}")
    if inv.accepted_at is not None:
        raise ValueError("invitation déjà acceptée")
    session.delete(inv)
    session.commit()


def invitation_is_expired(inv: Invitation) -> bool:
    if inv.accepted_at is not None:
        return True
    now = datetime.now(timezone.utc)
    return (now - inv.created_at) > INVITATION_TTL


def accept_invitation(
    session: Session, *, token: str, current_email: str
) -> Membership:
    """Vérifie que l'user courant match l'email invité, puis crée le membership.

    Renvoie le membership créé. Lève ValueError si le token est invalide, expiré,
    déjà utilisé, ou si l'email ne correspond pas.
    """
    inv = get_invitation_by_token(session, token)
    if inv is None:
        raise ValueError("token d'invitation invalide")
    if invitation_is_expired(inv):
        raise ValueError("invitation expirée ou déjà acceptée")
    if inv.email.strip().lower() != current_email.strip().lower():
        raise ValueError("cette invitation ne correspond pas à ton adresse email")

    user = session.execute(
        select(User).where(User.email == current_email.strip().lower())
    ).scalar_one()

    existing = session.get(Membership, (user.id, inv.org_id))
    if existing is None:
        m = Membership(user_id=user.id, org_id=inv.org_id, role=inv.role)
        session.add(m)
    else:
        if _ROLE_WEIGHT[inv.role] > _ROLE_WEIGHT[existing.role]:
            existing.role = inv.role
        m = existing

    inv.accepted_at = datetime.now(timezone.utc)
    session.commit()
    return m
