"""
Devis + gestion du pricing versionné (ADR-078 §3-4).

Heuristique de tokens (pré-scan, avant appel réel) :
    input_tokens  ≈ len(prompt) / 4
    output_tokens ≈ 500   (moyenne empirique pour une réponse factuelle)
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Model, ModelPricing, Test


OUTPUT_TOKENS_HEURISTIC = 500  # tokens de sortie moyens par appel


def get_pricing_at(session: Session, model_id: int, at: Optional[datetime] = None) -> Optional[ModelPricing]:
    """Renvoie le pricing en vigueur pour un modèle à une date donnée (défaut: now)."""
    ts = at or datetime.now(timezone.utc)
    return session.execute(
        select(ModelPricing).where(
            ModelPricing.model_id == model_id,
            ModelPricing.effective_from <= ts,
            (ModelPricing.effective_to.is_(None)) | (ModelPricing.effective_to > ts),
        ).order_by(ModelPricing.effective_from.desc())
    ).scalar_one_or_none()


def get_current_pricing(session: Session, model_id: int) -> Optional[ModelPricing]:
    return get_pricing_at(session, model_id)


def list_pricing(session: Session) -> list[dict[str, Any]]:
    """Renvoie le pricing courant pour tous les modèles (jointure)."""
    rows = session.execute(
        select(Model, ModelPricing)
        .join(ModelPricing, ModelPricing.model_id == Model.model_id, isouter=True)
        .where((ModelPricing.effective_to.is_(None)) | (ModelPricing.id.is_(None)))
        .order_by(Model.model_id)
    ).all()
    return [
        dict(
            model_id=m.model_id, model_name=m.model_name, model_version=m.model_version,
            pricing=p,
        )
        for m, p in rows
    ]


def set_pricing(
    session: Session, *, model_id: int, input_eur_per_1m: Decimal, output_eur_per_1m: Decimal
) -> ModelPricing:
    """Clôt la row active et en crée une nouvelle (versionnage transparent)."""
    now = datetime.now(timezone.utc)
    active = get_current_pricing(session, model_id)
    if active is not None:
        active.effective_to = now
    new = ModelPricing(
        model_id=model_id,
        input_price_per_1m_tokens=input_eur_per_1m,
        output_price_per_1m_tokens=output_eur_per_1m,
        currency="EUR",
        effective_from=now,
        effective_to=None,
    )
    session.add(new)
    session.commit()
    return new


def _estimate_tokens_for_prompt(prompt: str) -> int:
    return max(1, len(prompt or "") // 4)


def _resolve_model_for(session: Session, ref: Any) -> Optional[Model]:
    from run import resolve_model  # local import — cycle-safe

    try:
        return resolve_model(session, ref)
    except Exception:  # noqa: BLE001
        return None


def estimate_scan_cost(
    session: Session,
    *,
    org_id: int,
    tests: list[Test],
    tested_models: list[Any],
    judges: list[dict[str, Any]],
) -> dict[str, Any]:
    """Devis pré-scan (ADR-078 §3).

    tested_models : liste d'identifiants (model_id int ou model_version str).
    judges : liste `[{"model": "...", "repeats": N}]` (ADR-076 format).

    Renvoie un dict :
        {
          "total_eur": Decimal,
          "by_model": [{"model_version", "billed_to", "input_tokens", "output_tokens", "cost_eur"}],
          "unpriced": [<model_versions sans pricing>],
        }
    """
    from webapp import credentials  # local

    input_tokens_total = sum(_estimate_tokens_for_prompt(t.prompt) for t in tests)
    n_tests = len(tests)

    entries: list[dict[str, Any]] = []
    unpriced: list[str] = []

    def _line(model: Model, kind: str, factor_in: int, factor_out: int) -> None:
        pricing = get_current_pricing(session, model.model_id)
        cred = credentials.get_for_model(session, org_id, model.model_id)
        billed_to = "byok" if (cred and cred.is_active and cred.api_key_encrypted) else "platform"
        if pricing is None:
            unpriced.append(model.model_version)
            return
        in_t = input_tokens_total * factor_in
        out_t = OUTPUT_TOKENS_HEURISTIC * n_tests * factor_out
        cost = (
            Decimal(in_t) * pricing.input_price_per_1m_tokens / Decimal(1_000_000)
            + Decimal(out_t) * pricing.output_price_per_1m_tokens / Decimal(1_000_000)
        )
        entries.append(dict(
            model_version=model.model_version,
            kind=kind,
            billed_to=billed_to,
            input_tokens=in_t,
            output_tokens=out_t,
            cost_eur=cost.quantize(Decimal("0.0001")),
        ))

    for tm in tested_models:
        m = _resolve_model_for(session, tm)
        if m is None:
            continue
        _line(m, "tested", factor_in=1, factor_out=1)

    for j in judges:
        jm = _resolve_model_for(session, j.get("model") or j.get("model_id"))
        if jm is None:
            continue
        repeats = max(1, int(j.get("repeats", 1)))
        # Un juge est appelé 2x par test (réponse + citation).
        _line(jm, "judge", factor_in=2 * repeats, factor_out=2 * repeats)

    total = sum((e["cost_eur"] for e in entries), Decimal("0"))
    return dict(
        total_eur=total.quantize(Decimal("0.0001")),
        by_model=entries,
        unpriced=unpriced,
    )
