"""
Enregistrement row-par-appel de la consommation LLM (ADR-078 §5, ADR-080 §6.3).

Appelé depuis run.py::execute_run et evaluate.py::_record_judge_usage après
chaque appel réussi. Idempotent : chaque insert est une ligne.

Deux régimes de coût :
- coût RÉEL (OpenRouter, `usage.cost` en USD) → converti en EUR via USD_EUR_RATE,
  le brut étant conservé dans `cost_usd` pour audit/re-calcul ;
- coût ESTIMÉ (chemins directs : souverains, legacy) → pricing versionné
  `model_pricing` appliqué aux tokens (eux-mêmes heuristiques len/4 côté appelants).
"""
from __future__ import annotations

import os
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from models import UsageRecord
from webapp.pricing import get_current_pricing

_DEFAULT_USD_EUR_RATE = Decimal("0.88")


def usd_eur_rate() -> Decimal:
    """Taux de conversion USD→EUR appliqué à l'ingestion (ADR-080 §6.3)."""
    raw = os.environ.get("USD_EUR_RATE") or ""
    try:
        rate = Decimal(raw)
        if rate <= 0:
            raise ValueError
        return rate
    except Exception:  # noqa: BLE001 — valeur absente ou invalide → défaut
        return _DEFAULT_USD_EUR_RATE


def record(
    session: Session,
    *,
    org_id: int,
    model_id: int,
    run_id: Optional[int],
    kind: str,               # 'tested' | 'judge'
    billed_to: str,          # 'platform' | 'byok'
    input_tokens: int,
    output_tokens: int,
    cost_usd: Optional[Decimal] = None,  # coût réel provider (OpenRouter) si connu
) -> UsageRecord:
    if cost_usd is not None:
        cost = cost_usd * usd_eur_rate()
    else:
        pricing = get_current_pricing(session, model_id)
        if pricing is not None:
            cost = (
                Decimal(input_tokens) * pricing.input_price_per_1m_tokens / Decimal(1_000_000)
                + Decimal(output_tokens) * pricing.output_price_per_1m_tokens / Decimal(1_000_000)
            )
        else:
            cost = Decimal("0")
    row = UsageRecord(
        organization_id=org_id,
        model_id=model_id,
        run_id=run_id,
        kind=kind,
        billed_to=billed_to,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_eur=cost.quantize(Decimal("0.000001")),
        cost_usd=cost_usd.quantize(Decimal("0.000001")) if cost_usd is not None else None,
    )
    session.add(row)
    session.commit()
    return row
