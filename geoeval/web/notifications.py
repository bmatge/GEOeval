"""
Notifications des entités (ADR-089 §2.8, chantier E7).

Arbitrages E7 :
- **Destinataires par rôle selon le type** : membres DIRECTS de l'entité ayant l'un
  des rôles du type ; à défaut, ceux de l'ancêtre le plus proche qui en a (comme les
  alertes budgétaires E3). Pas encore d'abonnements partagés (avec les webhooks).
- **Préférences** : l'in-app est toujours actif ; l'email se règle par utilisateur et
  par type, avec un défaut par type.
- **Emails immédiats** : un message par événement, aux destinataires qui l'ont activé.

L'émission est idempotente par destinataire grâce à `dedup_key` : un détecteur peut
repasser autant de fois que nécessaire. Les détecteurs n'écrivent que des
notifications, jamais un résultat d'évaluation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from geoeval.db.models import Membership, Notification, NotificationPreference, Organization, User
from geoeval.observability import metrics
from geoeval.web import mailer

logger = logging.getLogger("geoeval.web.notifications")

EDITORS = ("org_admin", "editor")
ADMINS = ("org_admin",)


@dataclass(frozen=True)
class Kind:
    label: str
    roles: tuple[str, ...]
    email_default: bool
    description: str


KINDS: dict[str, Kind] = {
    "budget_threshold": Kind("Budget à 80 % ou atteint", ADMINS, True,
                             "Un plafond mensuel ou journalier de l'entité franchit 80 % ou 100 %."),
    "job_failed": Kind("Évaluation en échec", EDITORS, True,
                       "Une évaluation s'arrête sur une erreur (clé, quota, notateur indisponible…)."),
    "contract_expiring": Kind("Contrat LLM bientôt expiré", ADMINS, True,
                              "Un contrat de l'entité arrive à échéance dans 30 jours, puis 7 jours."),
    "contract_expired": Kind("Contrat LLM expiré", ADMINS, True,
                             "Un contrat actif de l'entité est échu : ses appels sont bloqués."),
    "always_wrong": Kind("Question toujours fausse", EDITORS, False,
                         "Une question reste sous le seuil de qualité sur plusieurs évaluations d'affilée "
                         "pour une même IA évaluée."),
}


# ---------------------------------------------------------------------
# Destinataires et préférences
# ---------------------------------------------------------------------
def recipients(session: Session, org: Organization, roles: Iterable[str]) -> tuple[list[User], Optional[Organization]]:
    """Membres directs de l'entité ayant l'un des rôles ; à défaut, ceux de l'ancêtre le
    plus proche qui en a. Renvoie (utilisateurs, entité dont ils sont membres)."""
    from geoeval.web.hierarchy import chain

    roles = tuple(roles)
    for node in chain(session, org):
        users = session.execute(
            select(User).join(Membership, Membership.user_id == User.id)
            .where(Membership.org_id == node.id, Membership.role.in_(roles))
            .order_by(User.email)
        ).scalars().all()
        if users:
            return list(users), node
    return [], None


def email_enabled(session: Session, user_id: int, kind: str) -> bool:
    pref = session.get(NotificationPreference, (user_id, kind))
    return pref.email if pref is not None else KINDS[kind].email_default


def preferences(session: Session, user_id: int) -> dict[str, bool]:
    """Préférence email effective de l'utilisateur pour chaque type."""
    rows = session.execute(
        select(NotificationPreference).where(NotificationPreference.user_id == user_id)
    ).scalars().all()
    own = {r.kind: r.email for r in rows}
    return {k: own.get(k, spec.email_default) for k, spec in KINDS.items()}


def set_preferences(session: Session, user_id: int, email_by_kind: dict[str, bool]) -> dict[str, bool]:
    unknown = set(email_by_kind) - set(KINDS)
    if unknown:
        raise ValueError(f"Types de notification inconnus : {sorted(unknown)}")
    for kind, email in email_by_kind.items():
        session.execute(
            insert(NotificationPreference).values(user_id=user_id, kind=kind, email=bool(email))
            .on_conflict_do_update(index_elements=["user_id", "kind"], set_={"email": bool(email)})
        )
    session.commit()
    return preferences(session, user_id)


# ---------------------------------------------------------------------
# Émission
# ---------------------------------------------------------------------
@dataclass
class Emission:
    created: list[Notification] = field(default_factory=list)
    recipients: list[str] = field(default_factory=list)       # emails des destinataires visés
    emailed: list[str] = field(default_factory=list)          # sous-ensemble ayant l'email activé
    # none (rien de nouveau) | sent | not_configured | failed | no_recipient | disabled
    email_status: str = "none"
    recipient_org: Optional[Organization] = None


def notify(
    session: Session, org: Organization, kind: str, *, title: str, body: str = "", link: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None, dedup_key: Optional[str] = None,
    email_subject: Optional[str] = None, email_body: Optional[str] = None,
) -> Emission:
    """Notifie les destinataires du type pour `org`. Avec `dedup_key`, un destinataire
    déjà notifié pour cette clé ne l'est pas deux fois. L'email part en un seul message
    aux nouveaux destinataires qui l'ont activé. Commite."""
    spec = KINDS[kind]
    users, member_of = recipients(session, org, spec.roles)
    out = Emission(recipients=[u.email for u in users], recipient_org=member_of)
    for u in users:
        new_id = session.execute(
            insert(Notification).values(
                user_id=u.id, organization_id=org.id, kind=kind, title=title, body=body, link=link,
                payload=payload, dedup_key=dedup_key,
            ).on_conflict_do_nothing(index_elements=["user_id", "dedup_key"],
                                     index_where=Notification.dedup_key.is_not(None))
            .returning(Notification.id)
        ).scalar_one_or_none()
        if new_id is None:
            continue
        n = session.get(Notification, new_id)
        out.created.append(n)
        if email_enabled(session, u.id, kind):
            out.emailed.append(u.email)
    session.commit()
    if out.created:
        metrics.NOTIFICATIONS.labels(kind).inc(len(out.created))
    if out.emailed:
        url = mailer.public_url(link) if link else None
        subject = email_subject or f"[GEOeval] {title}"
        text = email_body or (f"Bonjour,\n\n{body}\n\n" + (f"Détail : {url}\n\n" if url else "")
                              + "— GEOeval (message automatique)\n"
                              + "Préférences de notification : " + mailer.public_url("/notifications/preferences") + "\n")
        out.email_status = mailer.send(out.emailed, subject, text)
        emailed = set(out.emailed)
        for n in out.created:
            user = session.get(User, n.user_id)
            if user is not None and user.email in emailed:
                n.email_status = out.email_status
        session.commit()
    elif not users:
        out.email_status = "no_recipient"
    elif out.created:
        out.email_status = "disabled"          # tous les destinataires ont coupé l'email
    if out.created:
        logger.info("notification %s (%s) → %d destinataire(s), email=%s",
                    kind, org.slug, len(out.created), out.email_status)
    return out


def notify_safely(org_id: Optional[int], kind: str, **kwargs: Any) -> int:
    """Variante sans exception, dans sa propre session (worker, planificateur)."""
    if org_id is None:
        return 0
    from geoeval.db.session import SessionLocal

    try:
        with SessionLocal() as session:
            org = session.get(Organization, org_id)
            return len(notify(session, org, kind, **kwargs).created) if org is not None else 0
    except Exception:  # noqa: BLE001 — une notification ne doit jamais casser l'appelant
        logger.exception("notification %s en échec (org=%s)", kind, org_id)
        return 0


# ---------------------------------------------------------------------
# Lecture (boîte de réception)
# ---------------------------------------------------------------------
def unread_count(session: Session, user_id: int) -> int:
    return int(session.execute(
        select(func.count()).select_from(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
    ).scalar_one())


def list_for_user(session: Session, user_id: int, *, unread_only: bool = False, limit: int = 100) -> list[Notification]:
    stmt = select(Notification).where(Notification.user_id == user_id)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    return list(session.execute(stmt.order_by(Notification.created_at.desc(), Notification.id.desc())
                                .limit(limit)).scalars())


def mark_read(session: Session, user_id: int, notification_id: Optional[int] = None) -> int:
    """Marque une notification (ou toutes si `notification_id` est None) comme lue.
    Ne touche jamais celles d'un autre utilisateur. Renvoie le nombre de lignes."""
    stmt = update(Notification).where(Notification.user_id == user_id, Notification.read_at.is_(None))
    if notification_id is not None:
        stmt = stmt.where(Notification.id == notification_id)
    n = session.execute(stmt.values(read_at=func.now())).rowcount
    session.commit()
    return int(n or 0)
