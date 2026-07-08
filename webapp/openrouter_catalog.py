"""
Import du catalogue public OpenRouter (EPIC-001 Phase 5, ADR-080 §6.3).

- S5.1 : fetch à la demande de GET https://openrouter.ai/api/v1/models
  (API publique, sans clé) — jamais au boot, jamais bloquant pour l'app.
- S5.2 : conversion des prix OpenRouter (USD **par token**, chaînes décimales)
  en EUR **par million de tokens** via USD_EUR_RATE, puis versionnement dans
  `model_pricing` (clôture de la row active + création d'une neuve, ADR-076).

Decimal partout — jamais de float sur un prix.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Model
from webapp.pricing import get_current_pricing, set_pricing
from webapp.usage import usd_eur_rate

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
FETCH_TIMEOUT_S = 10.0

#: Précision de stockage de model_pricing (Numeric(12,6)) — sert aussi de
#: granularité de comparaison « prix identique » (idempotence S5.2).
PRICE_QUANT = Decimal("0.000001")


class CatalogError(RuntimeError):
    """Erreur de récupération ou de parsing du catalogue OpenRouter."""


@dataclass(frozen=True)
class CatalogEntry:
    """Un modèle du catalogue OpenRouter (prix USD/token, jamais float)."""

    id: str
    name: str
    context_length: Optional[int]
    supported_parameters: tuple[str, ...]
    prompt_usd_per_token: Optional[Decimal]      # None = prix indisponible
    completion_usd_per_token: Optional[Decimal]  # (absent ou négatif = dynamique)

    @property
    def prompt_eur_per_1m(self) -> Optional[Decimal]:
        return usd_token_to_eur_per_1m(self.prompt_usd_per_token)

    @property
    def completion_eur_per_1m(self) -> Optional[Decimal]:
        return usd_token_to_eur_per_1m(self.completion_usd_per_token)

    @property
    def has_native_search(self) -> bool:
        """Web search natif exposé par le modèle (sinon plugin Exa, ADR-080 §6.1)."""
        return "web_search_options" in self.supported_parameters


def parse_price(raw: object) -> Optional[Decimal]:
    """Parse un prix USD/token (chaîne décimale OpenRouter) en Decimal.

    Renvoie None si absent, non décimal ou négatif (prix « dynamique »).
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        return None
    return value if value >= 0 else None


def usd_token_to_eur_per_1m(usd_per_token: Optional[Decimal]) -> Optional[Decimal]:
    """USD/token (OpenRouter) → EUR/1M tokens au taux USD_EUR_RATE (ADR-080 §6.3)."""
    if usd_per_token is None or usd_per_token < 0:
        return None
    return (usd_per_token * Decimal(1_000_000) * usd_eur_rate()).quantize(PRICE_QUANT)


def fetch_catalog(timeout: float = FETCH_TIMEOUT_S) -> list[CatalogEntry]:
    """Récupère le catalogue public OpenRouter (à la demande uniquement).

    Lève CatalogError si l'API est injoignable ou la réponse inattendue.
    """
    req = urllib.request.Request(
        OPENROUTER_MODELS_URL,
        headers={"Accept": "application/json", "User-Agent": "GEOeval"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — URL constante https
            payload = json.load(resp)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as exc:
        raise CatalogError(f"Catalogue OpenRouter injoignable : {exc}") from exc

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise CatalogError("Réponse OpenRouter inattendue (champ `data` absent).")

    entries: list[CatalogEntry] = []
    for item in data:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        pricing = item.get("pricing") or {}
        try:
            context_length = int(item["context_length"]) if item.get("context_length") else None
        except (TypeError, ValueError):
            context_length = None
        entries.append(CatalogEntry(
            id=str(item["id"]),
            name=str(item.get("name") or item["id"]),
            context_length=context_length,
            supported_parameters=tuple(str(p) for p in (item.get("supported_parameters") or [])),
            prompt_usd_per_token=parse_price(pricing.get("prompt")),
            completion_usd_per_token=parse_price(pricing.get("completion")),
        ))
    entries.sort(key=lambda e: e.id)
    return entries


def existing_openrouter_models(session: Session) -> dict[str, Model]:
    """Modèles GEOeval de la famille openrouter, indexés par id catalogue."""
    rows = session.execute(
        select(Model).where(Model.model_name == "openrouter")
    ).scalars().all()
    return {m.model_version: m for m in rows}


def get_openrouter_model(session: Session, catalog_id: str) -> Optional[Model]:
    """Modèle GEOeval correspondant à un id catalogue OpenRouter, s'il existe."""
    return session.execute(
        select(Model).where(
            Model.model_name == "openrouter",
            Model.model_version == catalog_id,
        )
    ).scalars().first()


def import_pricing(
    session: Session,
    *,
    model_id: int,
    prompt_usd_per_token: Optional[Decimal],
    completion_usd_per_token: Optional[Decimal],
) -> bool:
    """Alimente `model_pricing` depuis un prix catalogue OpenRouter (S5.2).

    Conversion EUR au taux USD_EUR_RATE. No-op (False) si prix indisponible
    ou identique à la version active ; sinon clôt la row active et en crée
    une neuve datée (True) — jamais d'update en place (ADR-076).
    """
    input_eur = usd_token_to_eur_per_1m(prompt_usd_per_token)
    output_eur = usd_token_to_eur_per_1m(completion_usd_per_token)
    if input_eur is None or output_eur is None:
        return False
    active = get_current_pricing(session, model_id)
    if (
        active is not None
        and Decimal(active.input_price_per_1m_tokens).quantize(PRICE_QUANT) == input_eur
        and Decimal(active.output_price_per_1m_tokens).quantize(PRICE_QUANT) == output_eur
    ):
        return False
    set_pricing(session, model_id=model_id, input_eur_per_1m=input_eur, output_eur_per_1m=output_eur)
    return True
