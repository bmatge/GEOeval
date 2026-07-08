"""
Enregistrement row-par-appel de la consommation LLM (ADR-078 §5).

Appelé depuis run.py::call_tested_llm et evaluate.py::call_judge_llm après
chaque appel réussi. Idempotent : chaque insert est une ligne.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from models import UsageRecord
from webapp.pricing import get_current_pricing


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
) -> UsageRecord:
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
    )
    session.add(row)
    session.commit()
    return row
