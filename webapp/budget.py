"""
Budgets par organisation (ADR-078 §5, EPIC-001 Phase 3) — soft-stop.

Deux plafonds : mensuel (obligatoire dès qu'un budget est posé) et
journalier (optionnel, NULL = illimité). `check_budget(estimate)` refuse
un nouveau scan si spent + estimate dépasse l'un des deux caps. Un scan
déjà en cours va au bout même s'il dépasse à mi-parcours (préserve
l'historique — ADR-076).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import Budget, UsageRecord

Period = Literal["day", "month"]

_PERIOD_LABELS: dict[str, str] = {"day": "journalier", "month": "mensuel"}


@dataclass
class BudgetCheck:
    ok: bool
    reason: str
    spent_eur: Decimal  # dépensé sur le mois (compat historique)
    cap_eur: Optional[Decimal]  # plafond mensuel (compat historique)
    estimate_eur: Decimal
    spent_day_eur: Decimal = Decimal("0")
    daily_cap_eur: Optional[Decimal] = None


def get_budget(session: Session, org_id: int) -> Optional[Budget]:
    return session.get(Budget, org_id)


def set_cap(
    session: Session,
    *,
    org_id: int,
    cap_eur: Decimal,
    updated_by: Optional[int],
    daily_cap_eur: Optional[Decimal] = None,
) -> Budget:
    """Pose les plafonds de l'org (mensuel requis, journalier optionnel — None = illimité)."""
    b = session.get(Budget, org_id)
    if b is None:
        b = Budget(
            organization_id=org_id,
            monthly_cap_eur=cap_eur,
            daily_cap_eur=daily_cap_eur,
            currency="EUR",
            updated_by=updated_by,
        )
        session.add(b)
    else:
        b.monthly_cap_eur = cap_eur
        b.daily_cap_eur = daily_cap_eur
        b.updated_by = updated_by
    session.commit()
    return b


def current_period_spent(session: Session, org_id: int, period: Period) -> Decimal:
    """Somme cost_eur pour l'org depuis le début de la période calendaire ('day' | 'month')."""
    if period not in _PERIOD_LABELS:
        raise ValueError(f"Période inconnue : {period!r} (attendu 'day' ou 'month').")
    spent = session.execute(
        select(func.coalesce(func.sum(UsageRecord.cost_eur), 0)).where(
            UsageRecord.organization_id == org_id,
            UsageRecord.ts >= func.date_trunc(period, func.now()),
        )
    ).scalar_one()
    return Decimal(str(spent or 0))


def current_month_spent(session: Session, org_id: int) -> Decimal:
    """Somme cost_eur pour l'org, depuis le début du mois calendaire (compat)."""
    return current_period_spent(session, org_id, "month")


def check_budget(
    session: Session, *, org_id: int, estimate_eur: Decimal
) -> BudgetCheck:
    """Soft-stop : refuse si l'estimation dépasse le cap journalier OU mensuel.

    Chaque cap est ignoré s'il est NULL ; le message indique quel plafond bloque.
    """
    b = get_budget(session, org_id)
    spent_month = current_period_spent(session, org_id, "month")
    spent_day = current_period_spent(session, org_id, "day")
    if b is None:
        # Pas de cap posé : on laisse passer.
        return BudgetCheck(True, "no_cap", spent_month, None, estimate_eur, spent_day, None)

    monthly_cap = Decimal(str(b.monthly_cap_eur)) if b.monthly_cap_eur is not None else None
    daily_cap = Decimal(str(b.daily_cap_eur)) if b.daily_cap_eur is not None else None

    blocking: list[str] = []
    if daily_cap is not None and spent_day + estimate_eur > daily_cap:
        blocking.append(
            f"Budget journalier dépassé : {spent_day} + {estimate_eur} > {daily_cap} EUR."
        )
    if monthly_cap is not None and spent_month + estimate_eur > monthly_cap:
        blocking.append(
            f"Budget mensuel dépassé : {spent_month} + {estimate_eur} > {monthly_cap} EUR."
        )
    if blocking:
        return BudgetCheck(
            False, " ".join(blocking),
            spent_month, monthly_cap, estimate_eur, spent_day, daily_cap,
        )
    return BudgetCheck(True, "ok", spent_month, monthly_cap, estimate_eur, spent_day, daily_cap)
