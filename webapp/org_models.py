"""
DAO org_models (EPIC-001 Phase 4, S4.1) — allowlist de modèles par organisation.

Sémantique rétro-compatible (défaut proposé par l'epic, acté ici) :
    * org SANS AUCUNE ligne `org_models` → **héritage du catalogue global**
      filtré `models.is_active` : tout le catalogue actif est proposé aux
      editor/viewer, y compris les modèles ajoutés plus tard par l'admin
      plateforme (comportement identique à l'avant-Phase 4) ;
    * org avec au moins une ligne → seuls les modèles listés avec
      `is_active=TRUE` sont proposés. Une ligne `is_active=FALSE` masque
      explicitement le modèle (permet une allowlist vide non ambiguë).

Les org_admin et admins plateforme ne sont jamais filtrés : ils voient tout
le catalogue actif (c'est eux qui gèrent la liste, cf. S4.2).
"""
from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import delete as sa_delete, select
from sqlalchemy.orm import Session

from models import Model, OrgModel


def list_for_org(session: Session, org_id: int) -> list[OrgModel]:
    """Toutes les lignes d'allowlist d'une org (actives ou non)."""
    return list(
        session.execute(
            select(OrgModel)
            .where(OrgModel.organization_id == org_id)
            .order_by(OrgModel.model_id)
        ).scalars().all()
    )


def has_allowlist(session: Session, org_id: int) -> bool:
    """True si l'org a posé une allowlist (au moins une ligne, même inactive)."""
    return session.execute(
        select(OrgModel.id).where(OrgModel.organization_id == org_id).limit(1)
    ).scalar_one_or_none() is not None


def allowed_model_ids(session: Session, org_id: int) -> Optional[set[int]]:
    """Ids des modèles autorisés, ou None si héritage du catalogue global.

    None ≠ set() : None = aucune allowlist posée (tout le catalogue actif est
    visible), set() = allowlist posée qui n'autorise rien.
    """
    rows = list_for_org(session, org_id)
    if not rows:
        return None
    return {r.model_id for r in rows if r.is_active}


def filter_models(session: Session, org_id: int, models: Iterable[Model]) -> list[Model]:
    """Applique l'allowlist de l'org à une liste de modèles du catalogue.

    Sans allowlist, la liste est renvoyée telle quelle (héritage global).
    """
    allowed = allowed_model_ids(session, org_id)
    if allowed is None:
        return list(models)
    return [m for m in models if m.model_id in allowed]


def set_allowed(session: Session, org_id: int, model_id: int, allowed: bool) -> OrgModel:
    """Pose (upsert) l'autorisation d'UN modèle pour une org."""
    row = session.execute(
        select(OrgModel).where(
            OrgModel.organization_id == org_id,
            OrgModel.model_id == model_id,
        )
    ).scalar_one_or_none()
    if row is None:
        row = OrgModel(organization_id=org_id, model_id=model_id, is_active=allowed)
        session.add(row)
    else:
        row.is_active = allowed
    session.commit()
    return row


def replace_allowlist(session: Session, org_id: int, allowed_ids: set[int]) -> None:
    """Pose l'allowlist complète depuis le formulaire org_admin (S4.2).

    Écrit une ligne par modèle ACTIF du catalogue global (is_active=coché),
    de sorte qu'une sélection vide reste une allowlist explicite (rien de
    visible) et non un retour à l'héritage. Les lignes portant sur des
    modèles désactivés du catalogue sont conservées telles quelles.
    """
    catalog = session.execute(
        select(Model).where(Model.is_active.is_(True))
    ).scalars().all()
    existing = {r.model_id: r for r in list_for_org(session, org_id)}
    for m in catalog:
        wanted = m.model_id in allowed_ids
        row = existing.get(m.model_id)
        if row is None:
            session.add(OrgModel(
                organization_id=org_id, model_id=m.model_id, is_active=wanted,
            ))
        elif row.is_active != wanted:
            row.is_active = wanted
    session.commit()


def clear_allowlist(session: Session, org_id: int) -> None:
    """Supprime toutes les lignes → retour à l'héritage du catalogue global."""
    session.execute(sa_delete(OrgModel).where(OrgModel.organization_id == org_id))
    session.commit()
