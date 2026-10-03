"""
Budgets consolidés dans l'arbre d'entités (ADR-078 §5, ADR-089 §2.3, chantier E3) — soft-stop.

Chaque entité peut poser un plafond mensuel (obligatoire dès qu'un budget est posé)
et un plafond journalier (optionnel). Un plafond s'applique à la dépense
**consolidée** de l'entité : la sienne plus celle de tout son sous-arbre. Les
plafonds des sous-entités sont optionnels ; une entité sans budget propre est
contrainte par ceux de ses ancêtres.

`check_budget(estimate)` refuse un nouveau scan si UN seul plafond de la chaîne
(l'entité et ses ancêtres) serait dépassé, et nomme l'entité en cause. Un scan déjà
en cours va au bout même s'il dépasse à mi-parcours (historique préservé, ADR-076).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from geoeval.db.models import Budget, Organization, UsageRecord

Period = Literal["day", "month"]

_PERIOD_LABELS: dict[str, str] = {"day": "journalier", "month": "mensuel"}
WARNING_RATIO = Decimal("0.8")
EXCEEDED_RATIO = Decimal("1")


@dataclass
class Constraint:
    """Un plafond applicable à une entité (le sien ou celui d'un ancêtre)."""

    owner: Organization          # entité qui porte le plafond
    period: str                  # "month" | "day"
    cap_eur: Decimal
    spent_eur: Decimal           # dépense consolidée du sous-arbre de `owner`
    inherited: bool = False      # porté par un ancêtre

    @property
    def ratio(self) -> Optional[Decimal]:
        if self.cap_eur <= 0:
            return None if self.spent_eur <= 0 else Decimal("1")
        return self.spent_eur / self.cap_eur

    @property
    def pct(self) -> Optional[int]:
        r = self.ratio
        return None if r is None else min(999, int(r * 100))

    @property
    def level(self) -> str:
        """« ok », « warning » (≥ 80 %) ou « exceeded » (≥ 100 %). Un plafond à 0
        est « exceeded » : aucun scan possible."""
        if self.cap_eur <= 0:
            return "exceeded"
        r = self.ratio
        if r >= EXCEEDED_RATIO:
            return "exceeded"
        if r >= WARNING_RATIO:
            return "warning"
        return "ok"

    @property
    def period_label(self) -> str:
        return _PERIOD_LABELS[self.period]


@dataclass
class BudgetCheck:
    ok: bool
    reason: str
    spent_eur: Decimal  # dépense consolidée du mois de l'entité (compat historique)
    cap_eur: Optional[Decimal]  # plafond mensuel propre (compat historique)
    estimate_eur: Decimal
    spent_day_eur: Decimal = Decimal("0")
    daily_cap_eur: Optional[Decimal] = None
    blocking_org_id: Optional[int] = None
    constraints: list[Constraint] = field(default_factory=list)


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
    """Pose les plafonds de l'entité (mensuel requis, journalier optionnel — None = illimité)."""
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


def _check_period(period: str) -> None:
    if period not in _PERIOD_LABELS:
        raise ValueError(f"Période inconnue : {period!r} (attendu 'day' ou 'month').")


def own_period_spent(session: Session, org_id: int, period: Period) -> Decimal:
    """Dépense de l'entité SEULE depuis le début de la période calendaire."""
    _check_period(period)
    spent = session.execute(
        select(func.coalesce(func.sum(UsageRecord.cost_eur), 0)).where(
            UsageRecord.organization_id == org_id,
            UsageRecord.ts >= func.date_trunc(period, func.now()),
        )
    ).scalar_one()
    return Decimal(str(spent or 0))


def subtree_period_spent(session: Session, org: Organization, period: Period) -> Decimal:
    """Dépense consolidée (entité + sous-arbre) depuis le début de la période calendaire."""
    _check_period(period)
    spent = session.execute(
        select(func.coalesce(func.sum(UsageRecord.cost_eur), 0))
        .join(Organization, Organization.id == UsageRecord.organization_id)
        .where(
            Organization.path.like(org.path + "%"),
            UsageRecord.ts >= func.date_trunc(period, func.now()),
        )
    ).scalar_one()
    return Decimal(str(spent or 0))


def current_period_spent(session: Session, org_id: int, period: Period) -> Decimal:
    """Dépense consolidée de l'entité (elle + son sous-arbre) sur la période."""
    org = session.get(Organization, org_id)
    if org is None:
        return Decimal("0")
    return subtree_period_spent(session, org, period)


def current_month_spent(session: Session, org_id: int) -> Decimal:
    """Dépense consolidée du mois calendaire (compat)."""
    return current_period_spent(session, org_id, "month")


def constraints_for_owner(session: Session, owner: Organization, *, inherited: bool = False) -> list[Constraint]:
    """Plafonds posés par `owner` (mensuel puis journalier), avec sa dépense consolidée."""
    b = get_budget(session, owner.id)
    if b is None:
        return []
    out: list[Constraint] = []
    if b.monthly_cap_eur is not None:
        out.append(Constraint(owner, "month", Decimal(str(b.monthly_cap_eur)),
                              subtree_period_spent(session, owner, "month"), inherited))
    if b.daily_cap_eur is not None:
        out.append(Constraint(owner, "day", Decimal(str(b.daily_cap_eur)),
                              subtree_period_spent(session, owner, "day"), inherited))
    return out


def chain_constraints(session: Session, org: Organization) -> list[Constraint]:
    """Plafonds applicables à `org` : les siens puis ceux de ses ancêtres (proche → lointain)."""
    from geoeval.web.hierarchy import chain

    out: list[Constraint] = []
    for node in chain(session, org):
        out.extend(constraints_for_owner(session, node, inherited=node.id != org.id))
    return out


def chain_alerts(session: Session, org: Organization) -> list[Constraint]:
    """Plafonds de la chaîne au-dessus de 80 % (bandeaux d'alerte), les plus graves d'abord."""
    alerts = [c for c in chain_constraints(session, org) if c.level != "ok"]
    return sorted(alerts, key=lambda c: (c.level != "exceeded", c.inherited))


def check_budget(
    session: Session, *, org_id: int, estimate_eur: Decimal
) -> BudgetCheck:
    """Soft-stop : refuse si l'estimation ferait dépasser UN plafond de la chaîne
    (journalier ou mensuel, de l'entité ou d'un ancêtre). Le motif nomme l'entité
    dont le plafond bloque quand ce n'est pas l'entité elle-même."""
    org = session.get(Organization, org_id)
    if org is None:
        return BudgetCheck(True, "no_cap", Decimal("0"), None, estimate_eur)
    constraints = chain_constraints(session, org)
    own = get_budget(session, org_id)
    spent_month = subtree_period_spent(session, org, "month")
    spent_day = subtree_period_spent(session, org, "day")
    monthly_cap = Decimal(str(own.monthly_cap_eur)) if own is not None and own.monthly_cap_eur is not None else None
    daily_cap = Decimal(str(own.daily_cap_eur)) if own is not None and own.daily_cap_eur is not None else None

    blocking: list[str] = []
    blocking_org_id: Optional[int] = None
    # Journalier d'abord (plus contraignant à court terme), puis mensuel ; proche → lointain.
    for c in sorted(constraints, key=lambda c: c.period != "day"):
        if c.spent_eur + estimate_eur > c.cap_eur:
            owner = "" if not c.inherited else f" (plafond de « {c.owner.name} »)"
            blocking.append(
                f"Budget {c.period_label} dépassé{owner} : {c.spent_eur} + {estimate_eur} > {c.cap_eur} EUR."
            )
            blocking_org_id = blocking_org_id or c.owner.id
    if blocking:
        return BudgetCheck(False, " ".join(blocking), spent_month, monthly_cap, estimate_eur,
                           spent_day, daily_cap, blocking_org_id, constraints)
    reason = "ok" if constraints else "no_cap"
    return BudgetCheck(True, reason, spent_month, monthly_cap, estimate_eur, spent_day, daily_cap, None, constraints)
