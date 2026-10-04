"""
Notifications des entités (ADR-089 §2.8, chantier E7).

Arbitrages E7 :
- **Destinataires par rôle selon le type** : membres DIRECTS de l'entité ayant l'un
  des rôles du type ; à défaut, ceux de l'ancêtre le plus proche qui en a (comme les
  alertes budgétaires E3). Pas encore d'abonnements partagés (avec les webhooks).
- **Préférences** : l'in-app est toujours actif ; pour l'email, chacun choisit par type
  entre immédiat, récapitulatif quotidien ou aucun (suite E7), avec un défaut par type.
- **Emails immédiats** : un message par événement, aux destinataires qui l'ont choisi ;
  les autres reçoivent un récapitulatif chaque matin (GEOEVAL_DIGEST_TIME, 7 h 45).

L'émission est idempotente par destinataire grâce à `dedup_key` : un détecteur peut
repasser autant de fois que nécessaire. Les détecteurs n'écrivent que des
notifications, jamais un résultat d'évaluation.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone
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


MODES = ("immediate", "digest", "none")
MODE_LABELS = {"immediate": "Email immédiat", "digest": "Récapitulatif quotidien", "none": "Application seulement"}


@dataclass(frozen=True)
class Kind:
    label: str
    roles: tuple[str, ...]          # () : destinataires désignés explicitement (ex. l'auteur d'un signalement)
    default_mode: str               # immediate | digest | none
    description: str

    @property
    def email_default(self) -> bool:
        return self.default_mode != "none"


KINDS: dict[str, Kind] = {
    "budget_threshold": Kind("Budget à 80 % ou atteint", ADMINS, "immediate",
                             "Un plafond mensuel ou journalier de l'entité franchit 80 % ou 100 %."),
    "job_failed": Kind("Évaluation en échec", EDITORS, "immediate",
                       "Une évaluation s'arrête sur une erreur (clé, quota, notateur indisponible…)."),
    "contract_expiring": Kind("Contrat LLM bientôt expiré", ADMINS, "immediate",
                              "Un contrat de l'entité arrive à échéance dans 30 jours, puis 7 jours."),
    "contract_expired": Kind("Contrat LLM expiré", ADMINS, "immediate",
                             "Un contrat actif de l'entité est échu : ses appels sont bloqués."),
    "always_wrong": Kind("Question toujours fausse", EDITORS, "none",
                         "Une question reste sous le seuil de qualité sur plusieurs évaluations d'affilée "
                         "pour une même IA évaluée."),
    "citation_drop": Kind("Chute des citations officielles", EDITORS, "none",
                          "La part de citations vers les domaines officiels d'un périmètre chute nettement "
                          "pour une IA évaluée."),
    "report_opened": Kind("Signalement d'une question", EDITORS, "none",
                          "Une entité signale une question de l'entité (réponse attendue douteuse, citation "
                          "hors sujet…)."),
    "report_resolved": Kind("Réponse à un signalement", (), "none",
                            "L'entité propriétaire a traité un signalement que tu as fait."),
    "judge_disagreement": Kind("Notateur en désaccord avec le gold", EDITORS, "none",
                               "Sur le jeu de calibration de l'entité, l'accord d'un notateur avec les "
                               "annotations humaines passe sous le seuil (global ou pour un thème)."),
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


def email_mode(session: Session, user_id: int, kind: str) -> str:
    pref = session.get(NotificationPreference, (user_id, kind))
    return pref.mode if pref is not None else KINDS[kind].default_mode


def email_enabled(session: Session, user_id: int, kind: str) -> bool:
    """Email immédiat (le récapitulatif est géré à part)."""
    return email_mode(session, user_id, kind) == "immediate"


def preferences(session: Session, user_id: int) -> dict[str, str]:
    """Mode d'email effectif de l'utilisateur pour chaque type."""
    rows = session.execute(
        select(NotificationPreference).where(NotificationPreference.user_id == user_id)
    ).scalars().all()
    own = {r.kind: r.mode for r in rows}
    return {k: own.get(k, spec.default_mode) for k, spec in KINDS.items()}


def _mode(value: Any) -> str:
    if value is True:
        return "immediate"
    if value is False or value is None:
        return "none"
    if value not in MODES:
        raise ValueError(f"Mode d'email inconnu : {value!r} (attendu : {', '.join(MODES)}).")
    return value


def set_preferences(session: Session, user_id: int, mode_by_kind: dict[str, Any]) -> dict[str, str]:
    """Enregistre les modes (« immediate », « digest », « none » ; un booléen vaut
    immédiat / aucun). Types omis : inchangés."""
    unknown = set(mode_by_kind) - set(KINDS)
    if unknown:
        raise ValueError(f"Types de notification inconnus : {sorted(unknown)}")
    modes = {k: _mode(v) for k, v in mode_by_kind.items()}
    for kind, mode in modes.items():
        session.execute(
            insert(NotificationPreference).values(user_id=user_id, kind=kind, mode=mode)
            .on_conflict_do_update(index_elements=["user_id", "kind"], set_={"mode": mode})
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


def _email_text(title: str, body: str, link: Optional[str]) -> str:
    url = mailer.public_url(link) if link else None
    return (f"Bonjour,\n\n{body}\n\n" + (f"Détail : {url}\n\n" if url else "")
            + "— GEOeval (message automatique)\n"
            + "Préférences de notification : " + mailer.public_url("/notifications/preferences") + "\n")


def _emit(
    session: Session, org: Optional[Organization], kind: str, users: list[User], *, title: str, body: str,
    link: Optional[str], payload: Optional[dict[str, Any]], dedup_key: Optional[str],
    email_subject: Optional[str], email_body: Optional[str],
) -> Emission:
    out = Emission(recipients=[u.email for u in users])
    for u in users:
        mode = email_mode(session, u.id, kind)
        new_id = session.execute(
            insert(Notification).values(
                user_id=u.id, organization_id=org.id if org is not None else None, kind=kind, title=title,
                body=body, link=link, payload=payload, dedup_key=dedup_key,
                email_status="digest_pending" if mode == "digest" else "none",
            ).on_conflict_do_nothing(index_elements=["user_id", "dedup_key"],
                                     index_where=Notification.dedup_key.is_not(None))
            .returning(Notification.id)
        ).scalar_one_or_none()
        if new_id is None:
            continue
        out.created.append(session.get(Notification, new_id))
        if mode == "immediate":
            out.emailed.append(u.email)
    session.commit()
    if out.created:
        metrics.NOTIFICATIONS.labels(kind).inc(len(out.created))
    if out.emailed:
        out.email_status = mailer.send(out.emailed, email_subject or f"[GEOeval] {title}",
                                       email_body or _email_text(title, body, link))
        emailed = set(out.emailed)
        for n in out.created:
            user = session.get(User, n.user_id)
            if user is not None and user.email in emailed:
                n.email_status = out.email_status
        session.commit()
    elif not users:
        out.email_status = "no_recipient"
    elif out.created:
        out.email_status = "disabled"          # aucun destinataire en email immédiat
    if out.created:
        logger.info("notification %s (%s) → %d destinataire(s), email=%s",
                    kind, org.slug if org is not None else "-", len(out.created), out.email_status)
    return out


def notify(
    session: Session, org: Organization, kind: str, *, title: str, body: str = "", link: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None, dedup_key: Optional[str] = None,
    email_subject: Optional[str] = None, email_body: Optional[str] = None,
) -> Emission:
    """Notifie les destinataires du type pour `org` (par rôle). Avec `dedup_key`, un
    destinataire déjà notifié pour cette clé ne l'est pas deux fois. Email immédiat en un
    seul message à ceux qui l'ont choisi ; récapitulatif plus tard pour les autres. Commite."""
    users, member_of = recipients(session, org, KINDS[kind].roles)
    out = _emit(session, org, kind, users, title=title, body=body, link=link, payload=payload, dedup_key=dedup_key,
                email_subject=email_subject, email_body=email_body)
    out.recipient_org = member_of
    return out


def notify_users(
    session: Session, users: list[User], kind: str, *, org: Optional[Organization] = None, title: str,
    body: str = "", link: Optional[str] = None, payload: Optional[dict[str, Any]] = None,
    dedup_key: Optional[str] = None,
) -> Emission:
    """Notifie des utilisateurs désignés (ex. l'auteur d'un signalement), mêmes préférences."""
    return _emit(session, org, kind, [u for u in users if u is not None], title=title, body=body, link=link,
                 payload=payload, dedup_key=dedup_key, email_subject=None, email_body=None)


# ---------------------------------------------------------------------
# Récapitulatif quotidien
# ---------------------------------------------------------------------
DEFAULT_DIGEST_TIME = "07:45"


def digest_cutoff(now: datetime) -> datetime:
    """Notifications à inclure : créées avant la dernière échéance du récapitulatif
    (chaque jour à GEOEVAL_DIGEST_TIME, heure de Paris)."""
    from zoneinfo import ZoneInfo

    raw = (os.environ.get("GEOEVAL_DIGEST_TIME") or "").strip() or DEFAULT_DIGEST_TIME
    try:
        hh, mm = (int(x) for x in raw.split(":"))
        at = dtime(hh, mm)
    except ValueError:
        at = dtime(7, 45)
    paris = ZoneInfo("Europe/Paris")
    local = now.astimezone(paris)
    today = datetime.combine(local.date(), at, tzinfo=paris)
    cutoff = today if local >= today else today - timedelta(days=1)
    return cutoff.astimezone(timezone.utc)


def send_digests(session: Session, now: Optional[datetime] = None) -> int:
    """Envoie un récapitulatif par utilisateur pour ses notifications en attente créées
    avant l'échéance du jour. Idempotent sans état : chaque notification n'est traitée
    qu'une fois (statut). Les notifications déjà lues ne sont pas rappelées. Commite.
    Renvoie le nombre d'emails tentés."""
    now = now or datetime.now(timezone.utc)
    cutoff = digest_cutoff(now)
    pending = session.execute(
        select(Notification).where(Notification.email_status == "digest_pending", Notification.created_at < cutoff)
        .order_by(Notification.user_id, Notification.created_at)
    ).scalars().all()
    by_user: dict[int, list[Notification]] = {}
    for n in pending:
        if n.read_at is not None:
            n.email_status = "none"            # déjà vue dans l'application
            continue
        by_user.setdefault(n.user_id, []).append(n)
    sent = 0
    for user_id, items in by_user.items():
        user = session.get(User, user_id)
        if user is None:
            continue
        lines = [f"Bonjour,\n\n{len(items)} notification(s) depuis le dernier récapitulatif :\n"]
        for n in items:
            lines.append(f"• {n.title}" + (f"\n  {n.body}" if n.body else "")
                         + (f"\n  {mailer.public_url(n.link)}" if n.link else ""))
        lines.append("\nToutes tes notifications : " + mailer.public_url("/notifications")
                     + "\nPréférences : " + mailer.public_url("/notifications/preferences")
                     + "\n\n— GEOeval (récapitulatif quotidien)\n")
        status = mailer.send([user.email], f"[GEOeval] Récapitulatif : {len(items)} notification(s)", "\n".join(lines))
        for n in items:
            n.email_status = status
        sent += 1
    session.commit()
    if sent:
        logger.info("récapitulatifs envoyés : %d", sent)
    return sent


def send_digests_safely(session: Session) -> int:
    try:
        return send_digests(session)
    except Exception:  # noqa: BLE001 — le récapitulatif ne casse jamais le planificateur
        logger.exception("envoi des récapitulatifs en échec")
        session.rollback()
        return 0


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
