"""
Validation métier des questions (ADR-089 §2.9, fin de E8).

Arbitrages : **optionnelle, héritée, à quatre yeux** — un réglage par entité
(`organizations.review_required`, NULL = hériter, défaut : non requise). Quand il est actif :

- publier un brouillon (ou créer une question « publiée », ou réactiver une question
  retirée) la **soumet** : statut `in_review`, hors runs, pools et campagnes ;
- un **validateur** — org_admin de l'entité ou d'un ancêtre, ou membre désigné
  (`memberships.is_validator`, valable pour le sous-arbre) — l'**approuve** (publiée) ou la
  **renvoie** en brouillon avec un motif ; jamais l'auteur de la soumission ;
- modifier l'énoncé ou la réponse attendue d'une question publiée (ou en relecture) la
  renvoie en relecture : elle sort des runs jusqu'à l'approbation.

Notifications : `review_requested` aux validateurs (ceux de l'entité, à défaut ceux de
l'ancêtre le plus proche), `review_done` à l'auteur de la soumission. Sans le réglage,
le cycle brouillon → publiée → retirée reste inchangé.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from geoeval.db.models import Membership, Organization, Test, User
from geoeval.web import hierarchy, notifications

MAX_COMMENT = 2000


class ReviewError(ValueError):
    def __init__(self, detail: str, status: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status


# ---------------------------------------------------------------------
# Réglage hérité, validateurs
# ---------------------------------------------------------------------
def required(session: Session, org: Organization) -> bool:
    return bool(required_source(session, org)[0])


def required_source(session: Session, org: Organization) -> tuple[bool, Optional[Organization]]:
    """(validation requise ?, entité qui pose le réglage — None = défaut plateforme)."""
    r = hierarchy.resolve_nearest(session, org, lambda s, n: n.review_required)
    return (bool(r.value), r.source) if r.value is not None else (False, None)


def set_required(session: Session, org: Organization, value: Optional[bool]) -> None:
    """True / False : réglage propre ; None : hériter."""
    org.review_required = value
    session.commit()


def _chain_memberships(session: Session, org: Organization) -> list[tuple[Organization, list[Membership]]]:
    chain = hierarchy.chain(session, org)
    rows = session.execute(select(Membership).where(Membership.org_id.in_([o.id for o in chain]))).scalars().all()
    by_org: dict[int, list[Membership]] = {}
    for m in rows:
        by_org.setdefault(m.org_id, []).append(m)
    return [(o, by_org.get(o.id, [])) for o in chain]


def _is_validator_membership(m: Membership) -> bool:
    return m.role == "org_admin" or bool(m.is_validator)


def is_validator(session: Session, org: Organization, user_id: Optional[int], *, is_platform_admin: bool = False) -> bool:
    """Validateur pour les questions de `org` : org_admin ou validateur désigné sur l'entité ou un ancêtre."""
    if is_platform_admin:
        return True
    if user_id is None:
        return False
    return any(_is_validator_membership(m) and m.user_id == user_id
               for _, ms in _chain_memberships(session, org) for m in ms)


def validators(session: Session, org: Organization) -> list[User]:
    """Destinataires d'une demande de relecture : validateurs de l'entité, à défaut de l'ancêtre le plus proche."""
    for _, ms in _chain_memberships(session, org):
        ids = [m.user_id for m in ms if _is_validator_membership(m)]
        if ids:
            return list(session.execute(select(User).where(User.id.in_(ids)).order_by(User.email)).scalars())
    return []


def designated(session: Session, org: Organization) -> set[int]:
    """Utilisateurs désignés validateurs sur l'entité elle-même (hors org_admin d'office)."""
    return set(session.execute(select(Membership.user_id).where(
        Membership.org_id == org.id, Membership.is_validator.is_(True))).scalars())


def set_validator(session: Session, org: Organization, user_id: int, value: bool) -> None:
    m = session.get(Membership, (user_id, org.id))
    if m is None:
        raise ReviewError("Membre introuvable dans cette entité.", 404)
    m.is_validator = bool(value)
    session.commit()


# ---------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------
def _excerpt(test: Test) -> str:
    return test.prompt if len(test.prompt) <= 120 else test.prompt[:117] + "…"


def mark_submitted(session: Session, org: Organization, test: Test, user_id: Optional[int], *, reason: str) -> None:
    """Passe la question en relecture et prévient les validateurs (commit inclus)."""
    test.status = "in_review"
    test.validity_end_at = None
    test.submitted_by, test.submitted_at = user_id, datetime.now(timezone.utc)
    session.commit()
    recipients = [u for u in validators(session, org) if u.id != user_id]
    if recipients:
        notifications.notify_users(
            session, recipients, "review_requested", org=org,
            title=f"Question à relire — #{test.test_id}",
            body=f"« {_excerpt(test)} » ({reason}). Approuve-la ou renvoie-la en brouillon avec un motif.",
            link=f"/o/{org.slug}/reviews",
            payload={"test_id": test.test_id, "reason": reason},
            dedup_key=f"review_requested:{test.test_id}:{test.submitted_at.isoformat()}",
        )


def submit(session: Session, org: Organization, test: Test, user_id: Optional[int]) -> Test:
    """Brouillon → en relecture."""
    if test.organization_id != org.id:
        raise ReviewError("Question introuvable.", 404)
    if test.status != "draft":
        raise ReviewError("Seul un brouillon se soumet à la relecture.", 409)
    mark_submitted(session, org, test, user_id, reason="première publication")
    return test


def _check_reviewer(session: Session, org: Organization, test: Test, user_id: Optional[int],
                    is_platform_admin: bool) -> None:
    if test.organization_id != org.id:
        raise ReviewError("Question introuvable.", 404)
    if test.status != "in_review":
        raise ReviewError("Cette question n'est pas en relecture.", 409)
    if not is_validator(session, org, user_id, is_platform_admin=is_platform_admin):
        raise ReviewError("Réservé aux validateurs de l'entité.", 403)
    if test.submitted_by is not None and test.submitted_by == user_id:
        raise ReviewError("Quatre yeux : l'auteur de la soumission ne peut pas la valider lui-même.", 403)


def _notify_author(session: Session, org: Organization, test: Test, *, approved: bool) -> None:
    author = session.get(User, test.submitted_by) if test.submitted_by else None
    if author is None:
        return
    notifications.notify_users(
        session, [author], "review_done", org=org,
        title=f"Question #{test.test_id} {'approuvée' if approved else 'renvoyée en brouillon'}",
        body=(f"« {_excerpt(test)} » est publiée : elle entre dans les runs." if approved
              else f"« {_excerpt(test)} » revient en brouillon. Motif : {test.review_comment}"),
        link=f"/o/{org.slug}/tests/{test.test_id}",
        payload={"test_id": test.test_id, "approved": approved},
        dedup_key=f"review_done:{test.test_id}:{test.reviewed_at.isoformat()}",
    )


def approve(session: Session, org: Organization, test: Test, user_id: Optional[int], *,
            is_platform_admin: bool = False) -> Test:
    _check_reviewer(session, org, test, user_id, is_platform_admin)
    test.status, test.validity_end_at = "published", None
    test.reviewed_by, test.reviewed_at, test.review_comment = user_id, datetime.now(timezone.utc), None
    session.commit()
    _notify_author(session, org, test, approved=True)
    return test


def reject(session: Session, org: Organization, test: Test, user_id: Optional[int], comment: str, *,
           is_platform_admin: bool = False) -> Test:
    _check_reviewer(session, org, test, user_id, is_platform_admin)
    comment = (comment or "").strip()
    if not comment:
        raise ReviewError("Indique le motif du renvoi à l'auteur.")
    test.status = "draft"
    test.reviewed_by, test.reviewed_at, test.review_comment = user_id, datetime.now(timezone.utc), comment[:MAX_COMMENT]
    session.commit()
    _notify_author(session, org, test, approved=False)
    return test


def pending(session: Session, org: Organization) -> list[Test]:
    return list(session.execute(
        select(Test).where(Test.organization_id == org.id, Test.status == "in_review")
        .order_by(Test.submitted_at.asc().nullsfirst(), Test.test_id)
    ).scalars())
