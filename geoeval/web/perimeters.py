"""
DAO Périmètre (PR#18) — contexte de recherche par organisation.

Un périmètre appartient à une organisation ; ses questions (tests) et ses
programmations sont rattachées à lui. La slug est unique dans l'organisation.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional, Union
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from geoeval.db.models import Perimeter, Test


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,63}$")
_HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
MAX_DOMAINS = 50


# ---------------------------------------------------------------------
# Domaines officiels d'un site (E4)
# ---------------------------------------------------------------------
def host_of(value: str) -> str:
    """Hôte normalisé d'une URL ou d'un domaine : minuscules, sans schéma, chemin,
    port ni « www. » initial. Chaîne vide si rien d'exploitable."""
    value = (value or "").strip().lower()
    if not value:
        return ""
    if "://" not in value:
        value = "//" + value
    host = (urlsplit(value).hostname or "").rstrip(".")
    return host[4:] if host.startswith("www.") else host


def normalize_domains(raw: Union[str, Iterable[str], None]) -> list[str]:
    """Liste de domaines depuis une saisie libre (virgules, espaces, retours ligne)
    ou une liste. Dédoublonnée, triée. Lève ValueError si un domaine est invalide."""
    if raw is None:
        return []
    items = re.split(r"[\s,;]+", raw) if isinstance(raw, str) else list(raw)
    out: list[str] = []
    for item in items:
        if not (item or "").strip():
            continue
        host = host_of(item)
        if not _HOST_RE.fullmatch(host):
            raise ValueError(f"Domaine invalide : {item!r}.")
        if host not in out:
            out.append(host)
    if len(out) > MAX_DOMAINS:
        raise ValueError(f"{MAX_DOMAINS} domaines au plus par site.")
    return sorted(out)


def is_official(url: str, domains: Iterable[str]) -> bool:
    """Vrai si l'URL pointe sur un des domaines (ou un de leurs sous-domaines)."""
    host = host_of(url)
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in domains)


def official_share(urls: Iterable[str], domains: Iterable[str]) -> Optional[float]:
    """Part des citations qui pointent vers les domaines officiels (None si aucun
    domaine déclaré ou aucune citation)."""
    domains = list(domains)
    urls = [u for u in urls if u]
    if not domains or not urls:
        return None
    return sum(1 for u in urls if is_official(u, domains)) / len(urls)


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
    domains: Union[str, Iterable[str], None] = None,
) -> Perimeter:
    slug = _normalize_slug(slug)
    clean_domains = normalize_domains(domains)
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
        domains=clean_domains,
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
    domains: Union[str, Iterable[str], None] = None,
    set_domains: bool = False,
) -> Perimeter:
    p = get_by_id(session, org_id, perimeter_id)
    if p is None:
        raise ValueError(f"périmètre {perimeter_id} introuvable")
    clean_domains = normalize_domains(domains) if set_domains else None
    if set_domains:
        p.domains = clean_domains
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
