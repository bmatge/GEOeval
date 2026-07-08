"""
Budget mensuel par organisation (ADR-078 §5) — soft-stop.

`check_budget(estimate)` refuse un nouveau scan si spent + estimate > cap.
Un scan déjà en cours va au bout même s'il dépasse à mi-parcours (préserve
l'historique — ADR-076).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from models import Budget, UsageRecord


@dataclass
class BudgetCheck:
    ok: bool
    reason: str
    spent_eur: Decimal
    cap_eur: Optional[Decimal]
    estimate_eur: Decimal


def get_budget(session: Session, org_id: int) -> Optional[Budget]:
    return session.get(Budget, org_id)


def set_cap(
    session: Session, *, org_id: int, cap_eur: Decimal, updated_by: Optional[int]
) -> Budget:
    b = session.get(Budget, org_id)
    if b is None:
        b = Budget(
            organization_id=org_id,
            monthly_cap_eur=cap_eur,
            currency="EUR",
            updated_by=updated_by,
        )
        session.add(b)
    else:
        b.monthly_cap_eur = cap_eur
        b.updated_by = updated_by
    session.commit()
    return b


def current_month_spent(session: Session, org_id: int) -> Decimal:
    """Somme cost_eur pour l'org, depuis le début du mois calendaire."""
    spent = session.execute(
        select(func.coalesce(func.sum(UsageRecord.cost_eur), 0)).where(
            UsageRecord.organization_id == org_id,
            UsageRecord.ts >= func.date_trunc("month", func.now()),
        )
    ).scalar_one()
    return Decimal(str(spent or 0))


def check_budget(
    session: Session, *, org_id: int, estimate_eur: Decimal
) -> BudgetCheck:
    b = get_budget(session, org_id)
    spent = current_month_spent(session, org_id)
    if b is None:
        # Pas de cap posé : on laisse passer.
        return BudgetCheck(True, "no_cap", spent, None, estimate_eur)
    cap = Decimal(str(b.monthly_cap_eur))
    if spent + estimate_eur > cap:
        return BudgetCheck(
            False,
            f"Budget mensuel dépassé : {spent} + {estimate_eur} > {cap} EUR.",
            spent, cap, estimate_eur,
        )
    return BudgetCheck(True, "ok", spent, cap, estimate_eur)
