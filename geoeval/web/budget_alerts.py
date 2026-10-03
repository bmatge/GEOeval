"""
Alertes de seuil budgétaire (ADR-089 §2.3 / §2.8, chantier E3).

À chaque évaluation (fin de job, saut d'une planification), on parcourt les
plafonds de l'entité et de ses ancêtres. Pour chaque seuil franchi (80 %, 100 %)
pas encore signalé sur la période calendaire en cours, une ligne `budget_alerts`
est créée (unicité en base : jamais deux fois la même alerte) et un email part aux
org_admin de l'entité qui porte le plafond (à défaut, ceux de l'ancêtre le plus
proche qui en a). L'alerte reste visible dans l'application même sans SMTP.
E7 : l'envoi passe par les notifications (in-app + email selon les préférences).
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from geoeval.db.models import BudgetAlert, Organization
from geoeval.web import budget, mailer, notifications

logger = logging.getLogger("geoeval.web.budget_alerts")

THRESHOLDS = (80, 100)


def period_key(session: Session, period: str) -> str:
    """Clé de période calculée par la base (même horloge que la dépense consolidée)."""
    fmt = "YYYY-MM" if period == "month" else "YYYY-MM-DD"
    return session.execute(text("SELECT to_char(now(), :fmt)"), {"fmt": fmt}).scalar_one()


def thresholds_crossed(c: budget.Constraint) -> list[int]:
    if c.cap_eur <= 0:
        return [100] if c.spent_eur > 0 else []
    ratio = c.ratio or Decimal("0")
    return [t for t in THRESHOLDS if ratio * 100 >= t]


def recipients_for(session: Session, owner: Organization) -> list[str]:
    """org_admin directs de l'entité qui porte le plafond ; à défaut, ceux de
    l'ancêtre le plus proche qui en a (règle commune des notifications, E7)."""
    users, _ = notifications.recipients(session, owner, notifications.ADMINS)
    return [u.email for u in users]


def _compose(c: budget.Constraint, threshold: int) -> tuple[str, str]:
    what = "atteint" if threshold >= 100 else f"à {threshold} %"
    subject = f"[GEOeval] Budget {c.period_label} {what} — {c.owner.name}"
    link = mailer.public_url(f"/o/{c.owner.slug}/budget")
    body = (
        f"Bonjour,\n\n"
        f"Le plafond {c.period_label} de l'entité « {c.owner.name} » est {what}.\n\n"
        f"Dépense consolidée (entité et sous-entités) : {c.spent_eur:.2f} €\n"
        f"Plafond : {c.cap_eur:.2f} €\n\n"
        + ("Les nouveaux lancements et les exécutions programmées qui le dépasseraient sont refusés "
           "jusqu'à la prochaine période ou un relèvement du plafond.\n\n" if threshold >= 100 else
           "Au-delà de 100 %, les nouveaux lancements seront refusés.\n\n")
        + f"Suivi du budget : {link}\n\n— GEOeval (message automatique)\n"
    )
    return subject, body


def evaluate(session: Session, org_id: int) -> list[BudgetAlert]:
    """Crée et notifie les alertes nouvellement franchies pour l'entité et ses ancêtres.
    Renvoie les alertes créées par cet appel."""
    org = session.get(Organization, org_id)
    if org is None:
        return []
    created: list[BudgetAlert] = []
    for c in budget.chain_constraints(session, org):
        crossed = thresholds_crossed(c)
        if not crossed:
            continue
        key = period_key(session, c.period)
        for threshold in crossed:
            new_id = session.execute(
                insert(BudgetAlert)
                .values(organization_id=c.owner.id, period=c.period, period_key=key, threshold=threshold,
                        spent_eur=c.spent_eur, cap_eur=c.cap_eur)
                .on_conflict_do_nothing(index_elements=["organization_id", "period", "period_key", "threshold"])
                .returning(BudgetAlert.id)
            ).scalar_one_or_none()
            session.commit()
            if new_id is None:
                continue  # déjà signalée sur cette période
            alert = session.get(BudgetAlert, new_id)
            subject, body = _compose(c, threshold)
            # E7 : notification in-app des org_admin + email selon leurs préférences.
            what = "atteint" if threshold >= 100 else f"à {threshold} %"
            emission = notifications.notify(
                session, c.owner, "budget_threshold",
                title=f"Budget {c.period_label} {what} — {c.owner.name}",
                body=f"Dépense consolidée {c.spent_eur:.2f} € pour un plafond de {c.cap_eur:.2f} €.",
                link=f"/o/{c.owner.slug}/budget", dedup_key=f"budget_alert:{alert.id}",
                payload={"period": c.period, "period_key": key, "threshold": threshold,
                         "spent_eur": str(c.spent_eur), "cap_eur": str(c.cap_eur)},
                email_subject=subject, email_body=body,
            )
            alert.email_status = emission.email_status
            alert.emailed_to = emission.emailed or None
            session.commit()
            logger.warning("alerte budget %s %s %d %% (%s) : %.2f / %.2f €, email=%s",
                           c.owner.slug, c.period, threshold, key, c.spent_eur, c.cap_eur, alert.email_status)
            created.append(alert)
    return created


def evaluate_safely(org_id: Optional[int]) -> int:
    """Variante sans exception, dans sa propre session (fin de job, planificateur)."""
    if org_id is None:
        return 0
    from geoeval.db.session import SessionLocal

    try:
        with SessionLocal() as session:
            return len(evaluate(session, org_id))
    except Exception:  # noqa: BLE001 — une alerte ne doit jamais casser l'appelant
        logger.exception("évaluation des alertes budgétaires en échec (org=%s)", org_id)
        return 0


def recent_for_chain(session: Session, org: Organization, limit: int = 20) -> list[BudgetAlert]:
    """Alertes récentes des plafonds applicables à l'entité (elle et ses ancêtres)."""
    from geoeval.web.hierarchy import ids_from_path

    return list(session.execute(
        select(BudgetAlert).where(BudgetAlert.organization_id.in_(ids_from_path(org.path)))
        .order_by(BudgetAlert.created_at.desc()).limit(limit)
    ).scalars())
