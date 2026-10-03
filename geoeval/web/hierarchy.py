"""
Hiérarchie des entités et résolveur de paramètres hérités (ADR-089 §2.1-2.2, chantier E1).

Arbre ministère > direction > service stocké par `parent_id` + chemin matérialisé
`path` (``/12/45/78/``). Le chemin est la seule source pour les requêtes de
sous-arbre : descendants = ``path LIKE '/12/45/%'`` (index text_pattern_ops).

Le résolveur est l'unique porte d'entrée pour un paramètre hérité. Deux modes :
- `resolve_nearest` : la première valeur définie en remontant (soi d'abord) ;
- `resolve_restrictive` : combinaison de toutes les valeurs définies sur la chaîne
  (ex. intersection des listes blanches) — un enfant ne peut jamais élargir ce que
  ses ancêtres autorisent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Generic, Iterable, Optional, TypeVar

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from geoeval.db.models import Organization

T = TypeVar("T")

ORG_KINDS = ("ministere", "direction", "service", "autre")
KIND_LABELS = {"ministere": "Ministère", "direction": "Direction", "service": "Service", "autre": "Autre (non qualifié)"}
MAX_DEPTH = 6                     # 7 niveaux au plus, racine comprise
SIRET_RE = re.compile(r"^[0-9]{14}$")


class HierarchyError(ValueError):
    """Opération refusée sur l'arbre. `status` : 400 invalide, 404 introuvable, 409 conflit."""

    def __init__(self, detail: str, status: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status


# ---------------------------------------------------------------------
# Chemins
# ---------------------------------------------------------------------
def path_for(parent: Optional[Organization], org_id: int) -> str:
    return (parent.path if parent is not None else "/") + f"{org_id}/"


def ids_from_path(path: str) -> list[int]:
    """Identifiants de la racine jusqu'au nœud inclus."""
    return [int(x) for x in path.strip("/").split("/") if x]


def ancestor_ids(org: Organization) -> list[int]:
    """Identifiants des ancêtres stricts, de la racine au parent."""
    return ids_from_path(org.path)[:-1]


def normalize_kind(kind: Optional[str]) -> str:
    kind = (kind or "autre").strip().lower()
    if kind not in ORG_KINDS:
        raise HierarchyError(f"Type d'entité invalide : {kind!r} (attendu : {', '.join(ORG_KINDS)}).")
    return kind


def normalize_siret(siret: Optional[str]) -> Optional[str]:
    siret = re.sub(r"\s+", "", siret or "")
    if not siret:
        return None
    if not SIRET_RE.fullmatch(siret):
        raise HierarchyError("Le SIRET doit comporter exactement 14 chiffres.")
    return siret


# ---------------------------------------------------------------------
# Lecture
# ---------------------------------------------------------------------
def lineage(session: Session, org: Organization) -> list[Organization]:
    """Ancêtres stricts, de la racine au parent (fil d'Ariane)."""
    ids = ancestor_ids(org)
    if not ids:
        return []
    rows = {o.id: o for o in session.execute(select(Organization).where(Organization.id.in_(ids))).scalars()}
    return [rows[i] for i in ids if i in rows]


def chain(session: Session, org: Organization) -> list[Organization]:
    """Soi puis ancêtres, du plus proche au plus lointain (ordre de résolution)."""
    return [org] + list(reversed(lineage(session, org)))


def children(session: Session, org_id: int) -> list[Organization]:
    return list(session.execute(
        select(Organization).where(Organization.parent_id == org_id).order_by(Organization.name)
    ).scalars())


def descendants(session: Session, org: Organization, *, include_self: bool = False) -> list[Organization]:
    rows = list(session.execute(
        select(Organization)
        .where(Organization.path.like(org.path + "%"))
        .order_by(Organization.depth, Organization.name)
    ).scalars())
    return rows if include_self else [o for o in rows if o.id != org.id]


def descendant_ids(session: Session, org: Organization, *, include_self: bool = True) -> list[int]:
    return [o.id for o in descendants(session, org, include_self=include_self)]


def is_ancestor_or_self(candidate: Organization, org: Organization) -> bool:
    return org.path.startswith(candidate.path)


def tree(orgs: Iterable[Organization]) -> list[Organization]:
    """Ordre d'affichage en profondeur (parents puis enfants, frères par nom)."""
    by_parent: dict[Optional[int], list[Organization]] = {}
    items = list(orgs)
    ids = {o.id for o in items}
    for o in items:
        key = o.parent_id if o.parent_id in ids else None
        by_parent.setdefault(key, []).append(o)
    out: list[Organization] = []

    def walk(pid: Optional[int]) -> None:
        for o in sorted(by_parent.get(pid, []), key=lambda x: x.name.lower()):
            out.append(o)
            walk(o.id)

    walk(None)
    return out


# ---------------------------------------------------------------------
# Écriture
# ---------------------------------------------------------------------
def _next_id(session: Session) -> int:
    return int(session.execute(text("SELECT nextval(pg_get_serial_sequence('organizations', 'id'))")).scalar_one())


def _check_depth(parent: Optional[Organization], subtree_height: int = 0) -> int:
    depth = 0 if parent is None else parent.depth + 1
    if depth + subtree_height > MAX_DEPTH:
        raise HierarchyError(f"Profondeur maximale dépassée ({MAX_DEPTH + 1} niveaux au plus).")
    return depth


def new_organization(
    session: Session,
    *,
    name: str,
    slug: str,
    parent: Optional[Organization] = None,
    kind: Optional[str] = None,
    siret: Optional[str] = None,
    created_by: Optional[int] = None,
) -> Organization:
    """Construit une entité avec chemin et profondeur cohérents (sans commit)."""
    depth = _check_depth(parent)
    org_id = _next_id(session)
    org = Organization(
        id=org_id, name=name.strip(), slug=slug.strip(), created_by=created_by,
        parent_id=parent.id if parent is not None else None,
        kind=normalize_kind(kind), siret=normalize_siret(siret),
        path=path_for(parent, org_id), depth=depth,
    )
    session.add(org)
    return org


def validate_move(session: Session, org: Organization, new_parent: Optional[Organization]) -> int:
    """Vérifie un rattachement sans rien écrire ; renvoie la nouvelle profondeur.
    Lève HierarchyError (409 cycle, 400 profondeur)."""
    if new_parent is not None and is_ancestor_or_self(org, new_parent):
        raise HierarchyError("Rattachement impossible : le parent choisi appartient au sous-arbre de l'entité.", 409)
    subtree = descendants(session, org, include_self=True)
    height = max(o.depth for o in subtree) - org.depth
    return _check_depth(new_parent, height)


def move(session: Session, org: Organization, new_parent: Optional[Organization]) -> Organization:
    """Rattache `org` (et tout son sous-arbre) à `new_parent`, ou en fait une racine.

    Refuse les cycles (nouveau parent dans le sous-arbre de `org`) et le dépassement
    de profondeur. Réécrit `path` et `depth` de tout le sous-arbre en une transaction.
    """
    new_depth = validate_move(session, org, new_parent)
    if (new_parent.id if new_parent else None) == org.parent_id:
        return org
    subtree = descendants(session, org, include_self=True)
    old_prefix = org.path
    new_prefix = path_for(new_parent, org.id)
    delta = new_depth - org.depth
    session.execute(
        update(Organization)
        .where(Organization.path.like(old_prefix + "%"))
        .values(
            path=text(":new || substr(path, :cut)").bindparams(new=new_prefix, cut=len(old_prefix) + 1),
            depth=Organization.depth + delta,
        )
        .execution_options(synchronize_session=False)
    )
    org.parent_id = new_parent.id if new_parent is not None else None
    session.commit()
    for o in subtree:
        session.refresh(o)
    return org


def update_entity(
    session: Session,
    org: Organization,
    *,
    name: Optional[str] = None,
    kind: Optional[str] = None,
    siret: Optional[str] = None,
    set_siret: bool = False,
) -> Organization:
    """Met à jour nom, type et SIRET (le rattachement passe par `move`).
    Tout est validé avant la moindre écriture."""
    if name is not None and not name.strip():
        raise HierarchyError("Le nom de l'entité est obligatoire.")
    new_kind = normalize_kind(kind) if kind is not None else None
    new_siret = normalize_siret(siret) if set_siret else None
    if name is not None:
        org.name = name.strip()
    if new_kind is not None:
        org.kind = new_kind
    if set_siret:
        org.siret = new_siret
    session.commit()
    return org


def update_and_move(
    session: Session,
    org: Organization,
    *,
    name: Optional[str] = None,
    kind: Optional[str] = None,
    siret: Optional[str] = None,
    set_siret: bool = False,
    move_to: Optional[Organization] = None,
    do_move: bool = False,
) -> Organization:
    """Qualification + rattachement atomiques du point de vue de l'appelant :
    tout est validé (champs et rattachement) avant la première écriture."""
    if name is not None and not name.strip():
        raise HierarchyError("Le nom de l'entité est obligatoire.")
    if kind is not None:
        normalize_kind(kind)
    if set_siret:
        normalize_siret(siret)
    if do_move:
        validate_move(session, org, move_to)
    update_entity(session, org, name=name, kind=kind, siret=siret, set_siret=set_siret)
    if do_move:
        move(session, org, move_to)
    return org


# ---------------------------------------------------------------------
# Résolveur de paramètres hérités (ADR-089 §2.2)
# ---------------------------------------------------------------------
@dataclass
class Resolved(Generic[T]):
    value: Optional[T]
    source: Optional[Organization] = None                     # entité la plus proche ayant défini une valeur
    sources: list[Organization] = field(default_factory=list)  # toutes les entités contributrices, proche → lointain


def resolve_nearest(
    session: Session, org: Organization, getter: Callable[[Session, Organization], Optional[T]],
    *, include_self: bool = True,
) -> Resolved[T]:
    """Première valeur définie (non None) en remontant la chaîne."""
    for node in (chain(session, org) if include_self else chain(session, org)[1:]):
        value = getter(session, node)
        if value is not None:
            return Resolved(value=value, source=node, sources=[node])
    return Resolved(value=None)


def resolve_restrictive(
    session: Session, org: Organization, getter: Callable[[Session, Organization], Optional[T]],
    combine: Callable[[T, T], T], *, include_self: bool = True,
) -> Resolved[T]:
    """Combine toutes les valeurs définies sur la chaîne (ex. intersection) :
    restrictif par construction, un enfant ne relâche jamais une contrainte héritée."""
    value: Optional[T] = None
    sources: list[Organization] = []
    for node in (chain(session, org) if include_self else chain(session, org)[1:]):
        v = getter(session, node)
        if v is None:
            continue
        value = v if value is None else combine(value, v)
        sources.append(node)
    return Resolved(value=value, source=sources[0] if sources else None, sources=sources)
