"""Budget consolidé d'une entité (E3) — lecture, editor+."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from geoeval.web import budget, budget_alerts
from geoeval.web.api.deps import Principal, require_role
from geoeval.web.api.schemas import BudgetAlertOut, BudgetConstraintOut, BudgetOut
from geoeval.web.deps import get_db

router = APIRouter(prefix="/orgs/{org_slug}/budget", tags=["budget"])


@router.get("", response_model=BudgetOut, summary="Plafonds applicables (propres et hérités), dépense consolidée, alertes")
def get_budget(principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    org = principal.org
    own = budget.get_budget(db, org.id)
    constraints = budget.chain_constraints(db, org)
    return BudgetOut(
        monthly_cap_eur=float(own.monthly_cap_eur) if own is not None and own.monthly_cap_eur is not None else None,
        daily_cap_eur=float(own.daily_cap_eur) if own is not None and own.daily_cap_eur is not None else None,
        month_spent_eur=float(budget.subtree_period_spent(db, org, "month")),
        day_spent_eur=float(budget.subtree_period_spent(db, org, "day")),
        own_month_spent_eur=float(budget.own_period_spent(db, org.id, "month")),
        constraints=[
            BudgetConstraintOut(
                owner_slug=c.owner.slug, owner_name=c.owner.name, inherited=c.inherited, period=c.period,
                cap_eur=float(c.cap_eur), spent_eur=float(c.spent_eur),
                ratio=float(c.ratio) if c.ratio is not None else None, level=c.level,
            )
            for c in constraints
        ],
        alerts=[BudgetAlertOut.model_validate(a) for a in budget_alerts.recent_for_chain(db, org)],
    )
