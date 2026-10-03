"""
Pools de questions partagés par référence (ADR-089 §2.5, chantier E4).

Un pool appartient à une entité et regroupe des questions de cette entité. Il est
partagé selon sa visibilité :
- ``private`` : l'entité propriétaire seule ;
- ``descendants`` : l'entité propriétaire et tout son sous-arbre ;
- ``all`` : toutes les entités.

Un pool peut inclure d'autres pools (sans cycle). Une entité l'exécute en
l'**abonnant à un de ses périmètres** (arbitrage E4) : les questions effectives du
périmètre sont alors ses questions propres plus celles des pools abonnés.

Règle de sécurité : **la composition n'élargit jamais la visibilité**. Un pool
atteint par inclusion n'est suivi que s'il est lui-même visible de l'entité qui
consomme (celle du périmètre). Un pool devenu invisible (visibilité réduite)
cesse simplement de fournir des questions ; l'abonnement reste, signalé comme
inactif dans l'interface.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from sqlalchemy import delete as sa_delete, func, literal, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from geoeval.db.models import (
    Organization,
    Perimeter,
    PerimeterPool,
    PoolInclude,
    PoolQuestion,
    QuestionPool,
    Test,
)

VISIBILITIES = ("private", "descendants", "all")
VISIBILITY_LABELS = {
    "private": "Privé (cette entité)",
    "descendants": "Entité et sous-entités",
    "all": "Toutes les entités",
}
MAX_INCLUDE_DEPTH = 10


class PoolError(ValueError):
    """Opération refusée sur un pool. `status` : 400 invalide, 404 introuvable, 409 conflit."""

    def __init__(self, detail: str, status: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status


# ---------------------------------------------------------------------
# Visibilité
# ---------------------------------------------------------------------
def visible_to(pool: QuestionPool, owner: Organization, viewer: Organization) -> bool:
    if pool.visibility == "all" or owner.id == viewer.id:
        return True
    if pool.visibility == "descendants":
        return viewer.path.startswith(owner.path)
    return False


def _visible_clause(viewer: Organization):
    return or_(
        QuestionPool.owner_org_id == viewer.id,
        QuestionPool.visibility == "all",
        (QuestionPool.visibility == "descendants") & literal(viewer.path).like(Organization.path + "%"),
    )


def list_visible(session: Session, viewer: Organization) -> list[tuple[QuestionPool, Organization]]:
    """Pools visibles de l'entité : les siens d'abord, puis ceux des autres par nom."""
    rows = session.execute(
        select(QuestionPool, Organization)
        .join(Organization, Organization.id == QuestionPool.owner_org_id)
        .where(_visible_clause(viewer))
        .order_by(QuestionPool.owner_org_id != viewer.id, QuestionPool.name)
    ).all()
    return [(p, o) for p, o in rows]


def get(session: Session, pool_id: int) -> Optional[QuestionPool]:
    return session.get(QuestionPool, pool_id)


def owner_of(session: Session, pool: QuestionPool) -> Organization:
    return session.get(Organization, pool.owner_org_id)


def get_visible(session: Session, viewer: Organization, pool_id: int) -> tuple[QuestionPool, Organization]:
    """Pool visible de `viewer`, sinon 404 (pas de divulgation)."""
    pool = get(session, pool_id)
    if pool is None:
        raise PoolError("Pool introuvable.", 404)
    owner = owner_of(session, pool)
    if not visible_to(pool, owner, viewer):
        raise PoolError("Pool introuvable.", 404)
    return pool, owner


def get_owned(session: Session, owner: Organization, pool_id: int) -> QuestionPool:
    """Pool appartenant à `owner`, sinon 404."""
    pool = get(session, pool_id)
    if pool is None or pool.owner_org_id != owner.id:
        raise PoolError("Pool introuvable pour cette entité.", 404)
    return pool


# ---------------------------------------------------------------------
# Écriture
# ---------------------------------------------------------------------
def _check_visibility(visibility: str) -> str:
    visibility = (visibility or "private").strip().lower()
    if visibility not in VISIBILITIES:
        raise PoolError(f"Visibilité invalide : {visibility!r} (attendu : {', '.join(VISIBILITIES)}).")
    return visibility


def _check_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise PoolError("Le nom du pool est obligatoire.")
    if len(name) > 200:
        raise PoolError("Le nom du pool est limité à 200 caractères.")
    return name


def create(
    session: Session, owner: Organization, *, name: str, description: Optional[str] = None,
    visibility: str = "private", created_by: Optional[int] = None,
) -> QuestionPool:
    pool = QuestionPool(owner_org_id=owner.id, name=_check_name(name), description=(description or "").strip() or None,
                        visibility=_check_visibility(visibility), created_by=created_by)
    session.add(pool)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise PoolError(f"Un pool nommé « {name.strip()} » existe déjà pour cette entité.", 409)
    return pool


def update(
    session: Session, pool: QuestionPool, *, name: Optional[str] = None,
    description: Optional[str] = None, set_description: bool = False, visibility: Optional[str] = None,
) -> QuestionPool:
    new_name = _check_name(name) if name is not None else None
    new_visibility = _check_visibility(visibility) if visibility is not None else None
    if new_name is not None:
        pool.name = new_name
    if set_description:
        pool.description = (description or "").strip() or None
    if new_visibility is not None:
        pool.visibility = new_visibility
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise PoolError(f"Un pool nommé « {name.strip()} » existe déjà pour cette entité.", 409)
    return pool


def usage(session: Session, pool: QuestionPool) -> dict[str, int]:
    """Abonnements et inclusions qui référencent ce pool."""
    subs = session.execute(select(func.count()).select_from(PerimeterPool).where(PerimeterPool.pool_id == pool.id)).scalar_one()
    incl = session.execute(select(func.count()).select_from(PoolInclude).where(PoolInclude.child_pool_id == pool.id)).scalar_one()
    return {"subscriptions": int(subs), "included_by": int(incl)}


def delete(session: Session, pool: QuestionPool) -> dict[str, int]:
    """Supprime le pool ; abonnements et inclusions disparaissent avec lui (cascade).
    Les runs passés ne sont pas touchés : ils référencent les questions, pas le pool."""
    counts = usage(session, pool)
    session.delete(pool)
    session.commit()
    return counts


def pool_test_ids(session: Session, pool_id: int) -> list[int]:
    return list(session.execute(
        select(PoolQuestion.test_id).where(PoolQuestion.pool_id == pool_id).order_by(PoolQuestion.test_id)
    ).scalars())


def tests_by_ids(session: Session, test_ids: Iterable[int]) -> list[Test]:
    """Questions par identifiant, triées (affichage du contenu d'un pool)."""
    ids = sorted({int(t) for t in test_ids})
    if not ids:
        return []
    return list(session.execute(select(Test).where(Test.test_id.in_(ids)).order_by(Test.test_id)).scalars())


def add_tests(session: Session, pool: QuestionPool, test_ids: Iterable[int]) -> int:
    """Ajoute des questions de l'entité propriétaire. Renvoie le nombre ajouté."""
    ids = sorted({int(t) for t in test_ids})
    if not ids:
        return 0
    tests = session.execute(select(Test).where(Test.test_id.in_(ids))).scalars().all()
    found = {t.test_id: t for t in tests}
    foreign = [i for i in ids if i not in found or found[i].organization_id != pool.owner_org_id]
    if foreign:
        raise PoolError(f"Questions introuvables ou n'appartenant pas à l'entité propriétaire du pool : {foreign}")
    drafts = [i for i in ids if found[i].status == "draft"]
    if drafts:
        raise PoolError(f"Questions encore en brouillon : publie-les avant de les partager ({drafts}).")
    existing = set(pool_test_ids(session, pool.id))
    added = 0
    for i in ids:
        if i not in existing:
            session.add(PoolQuestion(pool_id=pool.id, test_id=i))
            added += 1
    session.commit()
    return added


def remove_test(session: Session, pool: QuestionPool, test_id: int) -> None:
    session.execute(sa_delete(PoolQuestion).where(PoolQuestion.pool_id == pool.id, PoolQuestion.test_id == test_id))
    session.commit()


def included_ids(session: Session, pool_id: int) -> list[int]:
    return list(session.execute(
        select(PoolInclude.child_pool_id).where(PoolInclude.parent_pool_id == pool_id).order_by(PoolInclude.child_pool_id)
    ).scalars())


def _reachable(session: Session, start_id: int) -> set[int]:
    """Pools atteignables depuis `start_id` par inclusions (sans le point de départ)."""
    seen: set[int] = set()
    frontier = [start_id]
    for _ in range(MAX_INCLUDE_DEPTH + 1):
        if not frontier:
            break
        children = session.execute(
            select(PoolInclude.child_pool_id).where(PoolInclude.parent_pool_id.in_(frontier))
        ).scalars().all()
        frontier = [c for c in children if c not in seen]
        seen.update(frontier)
    return seen


def include(session: Session, parent: QuestionPool, child: QuestionPool) -> None:
    """`parent` inclut `child`. Refuse : soi-même, un pool invisible du propriétaire
    du parent, un cycle."""
    if parent.id == child.id:
        raise PoolError("Un pool ne peut pas s'inclure lui-même.")
    parent_owner = owner_of(session, parent)
    if not visible_to(child, owner_of(session, child), parent_owner):
        raise PoolError("Pool introuvable.", 404)
    if parent.id in _reachable(session, child.id):
        raise PoolError("Inclusion impossible : elle créerait un cycle.", 409)
    if child.id in included_ids(session, parent.id):
        return
    session.add(PoolInclude(parent_pool_id=parent.id, child_pool_id=child.id))
    session.commit()


def exclude(session: Session, parent: QuestionPool, child_id: int) -> None:
    session.execute(sa_delete(PoolInclude).where(PoolInclude.parent_pool_id == parent.id, PoolInclude.child_pool_id == child_id))
    session.commit()


# ---------------------------------------------------------------------
# Abonnements de périmètres
# ---------------------------------------------------------------------
def subscribe(session: Session, perimeter: Perimeter, pool_id: int, *, created_by: Optional[int] = None) -> QuestionPool:
    viewer = session.get(Organization, perimeter.organization_id)
    pool, _owner = get_visible(session, viewer, pool_id)
    exists = session.get(PerimeterPool, (perimeter.id, pool.id))
    if exists is None:
        session.add(PerimeterPool(perimeter_id=perimeter.id, pool_id=pool.id, created_by=created_by))
        session.commit()
    return pool


def unsubscribe(session: Session, perimeter: Perimeter, pool_id: int) -> None:
    session.execute(sa_delete(PerimeterPool).where(PerimeterPool.perimeter_id == perimeter.id, PerimeterPool.pool_id == pool_id))
    session.commit()


@dataclass
class Subscription:
    pool: QuestionPool
    owner: Organization
    visible: bool                       # le pool est-il (encore) visible de l'entité du périmètre ?
    n_questions: int = 0                # questions effectivement fournies (inclusions comprises)


def subscriptions(session: Session, perimeter: Perimeter) -> list[Subscription]:
    viewer = session.get(Organization, perimeter.organization_id)
    rows = session.execute(
        select(QuestionPool, Organization)
        .join(PerimeterPool, PerimeterPool.pool_id == QuestionPool.id)
        .join(Organization, Organization.id == QuestionPool.owner_org_id)
        .where(PerimeterPool.perimeter_id == perimeter.id)
        .order_by(QuestionPool.name)
    ).all()
    out = []
    for pool, owner in rows:
        visible = visible_to(pool, owner, viewer)
        n = len(resolve_pool_tests(session, pool.id, viewer).test_ids) if visible else 0
        out.append(Subscription(pool, owner, visible, n))
    return out


# ---------------------------------------------------------------------
# Résolution des questions
# ---------------------------------------------------------------------
@dataclass
class Resolution:
    test_ids: set[int] = field(default_factory=set)
    origins: dict[int, list[str]] = field(default_factory=dict)   # test_id → noms des pools fournisseurs
    skipped_pool_ids: set[int] = field(default_factory=set)       # pools atteints mais invisibles


def resolve_pool_tests(session: Session, pool_id: int, viewer: Organization, *, _res: Optional[Resolution] = None,
                       _seen: Optional[set[int]] = None) -> Resolution:
    """Questions d'un pool et de ses inclusions, en ne suivant que les pools visibles
    de `viewer` (la composition n'élargit jamais la visibilité)."""
    res = _res or Resolution()
    seen = _seen if _seen is not None else set()
    frontier = [pool_id]
    depth = 0
    while frontier and depth <= MAX_INCLUDE_DEPTH:
        rows = session.execute(
            select(QuestionPool, Organization)
            .join(Organization, Organization.id == QuestionPool.owner_org_id)
            .where(QuestionPool.id.in_([f for f in frontier if f not in seen]))
        ).all()
        next_frontier: list[int] = []
        for pool, owner in rows:
            seen.add(pool.id)
            if not visible_to(pool, owner, viewer):
                res.skipped_pool_ids.add(pool.id)
                continue
            for tid in pool_test_ids(session, pool.id):
                res.test_ids.add(tid)
                res.origins.setdefault(tid, [])
                if pool.name not in res.origins[tid]:
                    res.origins[tid].append(pool.name)
            next_frontier.extend(c for c in included_ids(session, pool.id) if c not in seen)
        frontier = next_frontier
        depth += 1
    return res


def effective_resolution(session: Session, perimeter: Perimeter) -> Resolution:
    """Questions des pools abonnés au périmètre (sans ses questions propres)."""
    viewer = session.get(Organization, perimeter.organization_id)
    res = Resolution()
    seen: set[int] = set()
    sub_ids = session.execute(
        select(PerimeterPool.pool_id).where(PerimeterPool.perimeter_id == perimeter.id).order_by(PerimeterPool.pool_id)
    ).scalars().all()
    for pid in sub_ids:
        resolve_pool_tests(session, pid, viewer, _res=res, _seen=seen)
    return res


def effective_tests(
    session: Session, perimeter: Perimeter, *, active_only: bool = True, ready_only: bool = True,
    test_ids: Optional[Iterable[int]] = None,
) -> list[Test]:
    """Questions effectives d'un périmètre : ses questions propres + celles des pools
    abonnés, dédoublonnées, triées par identifiant. Filtres actif / prêt comme
    `load_tests`. `test_ids` restreint à une sous-sélection."""
    pooled = effective_resolution(session, perimeter).test_ids
    stmt = select(Test).where(or_(Test.perimeter_id == perimeter.id, Test.test_id.in_(pooled or [-1])))
    if test_ids is not None:
        stmt = stmt.where(Test.test_id.in_([int(t) for t in test_ids] or [-1]))
    if active_only:
        stmt = stmt.where(Test.validity_end_at.is_(None), Test.status == "published")
    if ready_only:
        stmt = stmt.where(Test.expected_answer.is_not(None))
    return list(session.execute(stmt.order_by(Test.test_id)).scalars())


def origins_for(session: Session, perimeter: Perimeter) -> dict[int, list[str]]:
    """Pour l'affichage : test_id → pools qui le fournissent (vide = question propre)."""
    return effective_resolution(session, perimeter).origins
