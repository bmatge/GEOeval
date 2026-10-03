"""
Catalogue global des thèmes (ADR-089 §2.5, chantier E4).

Géré par l'administration plateforme (arbitrage E4 : comparabilité entre
entités, pas de doublons). Les éditeurs appliquent les thèmes aux questions et
aux sites (périmètres) de leur entité.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable, Optional

from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from geoeval.db.models import PerimeterTheme, QuestionTheme, Theme

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{1,63}$")


class ThemeError(ValueError):
    """Opération refusée sur un thème. `status` : 400 invalide, 404 introuvable, 409 conflit."""

    def __init__(self, detail: str, status: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status


def slugify(label: str) -> str:
    s = unicodedata.normalize("NFKD", label or "").encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:64]


def list_all(session: Session) -> list[Theme]:
    return list(session.execute(select(Theme).order_by(Theme.label)).scalars())


def get(session: Session, theme_id: int) -> Optional[Theme]:
    return session.get(Theme, theme_id)


def create(session: Session, *, label: str, slug: Optional[str] = None) -> Theme:
    label = (label or "").strip()
    if not label:
        raise ThemeError("Le libellé du thème est obligatoire.")
    slug = (slug or "").strip().lower() or slugify(label)
    if not _SLUG_RE.fullmatch(slug):
        raise ThemeError(f"Slug invalide : {slug!r} (minuscules, chiffres, tirets ; 2 à 64 caractères).")
    theme = Theme(slug=slug, label=label)
    session.add(theme)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise ThemeError(f"Le thème {slug!r} existe déjà.", 409)
    return theme


def rename(session: Session, theme: Theme, label: str) -> Theme:
    label = (label or "").strip()
    if not label:
        raise ThemeError("Le libellé du thème est obligatoire.")
    theme.label = label
    session.commit()
    return theme


def usage(session: Session, theme_id: int) -> dict[str, int]:
    q = session.execute(select(func.count()).select_from(QuestionTheme).where(QuestionTheme.theme_id == theme_id)).scalar_one()
    p = session.execute(select(func.count()).select_from(PerimeterTheme).where(PerimeterTheme.theme_id == theme_id)).scalar_one()
    return {"questions": int(q), "perimeters": int(p)}


def delete(session: Session, theme: Theme) -> dict[str, int]:
    """Supprime le thème et ses étiquettes (cascade). Aucun résultat d'évaluation n'en dépend."""
    counts = usage(session, theme.id)
    session.delete(theme)
    session.commit()
    return counts


def validate_ids(session: Session, theme_ids: Iterable[int]) -> list[int]:
    """Identifiants dédoublonnés et triés ; ThemeError si l'un est inconnu."""
    ids = sorted({int(t) for t in theme_ids})
    if not ids:
        return []
    known = set(session.execute(select(Theme.id).where(Theme.id.in_(ids))).scalars())
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise ThemeError(f"Thèmes inconnus : {unknown}")
    return ids


def set_for_test(session: Session, test_id: int, theme_ids: Iterable[int]) -> list[int]:
    ids = validate_ids(session, theme_ids)
    session.execute(sa_delete(QuestionTheme).where(QuestionTheme.test_id == test_id))
    session.add_all([QuestionTheme(test_id=test_id, theme_id=i) for i in ids])
    session.commit()
    return ids


def set_for_perimeter(session: Session, perimeter_id: int, theme_ids: Iterable[int]) -> list[int]:
    ids = validate_ids(session, theme_ids)
    session.execute(sa_delete(PerimeterTheme).where(PerimeterTheme.perimeter_id == perimeter_id))
    session.add_all([PerimeterTheme(perimeter_id=perimeter_id, theme_id=i) for i in ids])
    session.commit()
    return ids


def for_tests(session: Session, test_ids: Iterable[int]) -> dict[int, list[Theme]]:
    ids = list({int(t) for t in test_ids})
    out: dict[int, list[Theme]] = {i: [] for i in ids}
    if not ids:
        return out
    rows = session.execute(
        select(QuestionTheme.test_id, Theme).join(Theme, Theme.id == QuestionTheme.theme_id)
        .where(QuestionTheme.test_id.in_(ids)).order_by(Theme.label)
    ).all()
    for tid, theme in rows:
        out[tid].append(theme)
    return out


def for_perimeter(session: Session, perimeter_id: int) -> list[Theme]:
    return list(session.execute(
        select(Theme).join(PerimeterTheme, PerimeterTheme.theme_id == Theme.id)
        .where(PerimeterTheme.perimeter_id == perimeter_id).order_by(Theme.label)
    ).scalars())


def test_ids_with_theme(session: Session, theme_id: int) -> set[int]:
    return set(session.execute(select(QuestionTheme.test_id).where(QuestionTheme.theme_id == theme_id)).scalars())
