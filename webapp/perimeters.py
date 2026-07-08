"""
DAO Périmètre (PR#18) — contexte de recherche par organisation.

Un périmètre appartient à une organisation ; ses questions (tests) et ses
programmations sont rattachées à lui. La slug est unique dans l'organisation.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import Perimeter, Test


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,63}$")


def _normalize_slug(slug: str) -> str:
    slug = (slug or "").strip().lower()
    if not _SLUG_RE.fullmatch(slug):
        raise ValueError(
            f"slug invalide {slug!r} : minuscules, chiffres, tirets (1–64 chars, "
            "commence par lettre/chiffre)."
        )
    return slug


def list_for_org(session: Session, org_id: int) -> list[Perimeter]:
    return list(
        session.execute(
            select(Perimeter)
            .where(Perimeter.organization_id == org_id)
            .order_by(Perimeter.name)
        ).scalars().all()
    )


def get_by_id(session: Session, org_id: int, perimeter_id: int) -> Optional[Perimeter]:
    p = session.get(Perimeter, perimeter_id)
    if p is None or p.organization_id != org_id:
        return None
    return p


def get_by_slug(session: Session, org_id: int, slug: str) -> Optional[Perimeter]:
    return session.execute(
        select(Perimeter).where(
            Perimeter.organization_id == org_id,
            Perimeter.slug == slug,
        )
    ).scalar_one_or_none()


def count_tests(session: Session, perimeter_id: int) -> int:
    return int(session.execute(
        select(func.count()).select_from(Test).where(Test.perimeter_id == perimeter_id)
    ).scalar_one())


def create(
    session: Session,
    *,
    org_id: int,
    name: str,
    slug: str,
    kind: Optional[str] = None,
    home_url: Optional[str] = None,
    description: Optional[str] = None,
    created_by: Optional[int] = None,
) -> Perimeter:
    slug = _normalize_slug(slug)
    if get_by_slug(session, org_id, slug) is not None:
        raise ValueError(f"un périmètre avec le slug {slug!r} existe déjà pour cette organisation.")
    p = Perimeter(
        organization_id=org_id,
        name=name.strip(),
        slug=slug,
        kind=(kind or None),
        home_url=(home_url or None),
        description=(description or None),
        created_by=created_by,
    )
    session.add(p)
    session.commit()
    return p


def update(
    session: Session,
    *,
    org_id: int,
    perimeter_id: int,
    name: str,
    kind: Optional[str] = None,
    home_url: Optional[str] = None,
    description: Optional[str] = None,
) -> Perimeter:
    p = get_by_id(session, org_id, perimeter_id)
    if p is None:
        raise ValueError(f"périmètre {perimeter_id} introuvable")
    p.name = name.strip()
    p.kind = (kind or None)
    p.home_url = (home_url or None)
    p.description = (description or None)
    session.commit()
    return p


def delete(session: Session, org_id: int, perimeter_id: int) -> None:
    """Supprime un périmètre vide (sans question rattachée). Le périmètre
    « Général » n'est jamais supprimable — il sert de refuge à la migration."""
    p = get_by_id(session, org_id, perimeter_id)
    if p is None:
        raise ValueError(f"périmètre {perimeter_id} introuvable")
    if p.slug == "general":
        raise ValueError("le périmètre « Général » n'est pas supprimable.")
    if count_tests(session, perimeter_id) > 0:
        raise ValueError(
            "ce périmètre contient encore des questions — déplace-les ou "
            "supprime-les d'abord."
        )
    session.delete(p)
    session.commit()


def move_test(
    session: Session,
    *,
    org_id: int,
    test_id: int,
    to_perimeter_id: int,
) -> Test:
    """Déplace une question vers un autre périmètre de la même org."""
    dest = get_by_id(session, org_id, to_perimeter_id)
    if dest is None:
        raise ValueError(f"périmètre cible {to_perimeter_id} introuvable")
    test = session.get(Test, test_id)
    if test is None or test.organization_id != org_id:
        raise ValueError(f"question {test_id} introuvable pour cette organisation")
    test.perimeter_id = to_perimeter_id
    session.commit()
    return test
