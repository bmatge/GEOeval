"""
Contrats LLM des entités (ADR-089 §2.6, chantier E5) — remplace org_credentials.

Un contrat (marché, clé d'API) est posé par une entité pour une famille de
fournisseurs et hérité par tout son sous-arbre. Il couvre toute la famille, ou
seulement les modèles de `model_ids` s'il en liste.

Résolution pour (entité, modèle) : on remonte la chaîne de l'entité ; au premier
niveau qui porte un contrat ACTIF couvrant le modèle, la recherche s'arrête.
- Un contrat dans sa période de validité et sous son plafond est utilisé (à un
  même niveau, un contrat restreint au modèle prime sur un contrat de famille).
- Sinon (expiré, pas encore commencé, plafond atteint) l'appel est **bloqué**
  (arbitrage E5) : jamais de repli silencieux sur l'ancêtre ou la plateforme.
Un contrat désactivé est ignoré (la cascade continue) : c'est l'échappatoire
explicite. Sans contrat sur la chaîne : configuration du modèle puis plateforme.

Seul ce module (avec crypto.py) manipule les blobs Fernet des contrats.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from geoeval.core.llm_clients import provider_family
from geoeval.db.models import LlmContract, Model, Organization, UsageRecord
from geoeval.web import hierarchy
from geoeval.web.crypto import decrypt_secret, encrypt_secret

FAMILIES = ("openrouter", "openai", "mistral", "gemini", "albert", "generic")
FAMILY_LABELS = {
    "openrouter": "OpenRouter",
    "openai": "OpenAI",
    "mistral": "Mistral AI",
    "gemini": "Google Gemini",
    "albert": "Albert (Etalab)",
    "generic": "Endpoint compatible OpenAI",
}
HOSTINGS = ("eu", "non_eu")
HOSTING_LABELS = {"eu": "Union européenne", "non_eu": "Hors UE", None: "Inconnu"}


class ContractError(ValueError):
    """Opération refusée sur un contrat. `status` : 400 invalide, 404 introuvable, 409 conflit."""

    def __init__(self, detail: str, status: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status


# ---------------------------------------------------------------------
# Règles pures
# ---------------------------------------------------------------------
def covers(contract: LlmContract, model: Any) -> bool:
    """Le contrat couvre-t-il ce modèle (même famille, et dans la restriction s'il y en a une) ?"""
    if contract.family != provider_family(getattr(model, "model_name", None)):
        return False
    ids = [int(i) for i in (contract.model_ids or [])]
    return not ids or int(model.model_id) in ids


def in_window(contract: LlmContract, day: date) -> bool:
    if contract.valid_from is not None and day < contract.valid_from:
        return False
    if contract.valid_to is not None and day > contract.valid_to:
        return False
    return True


def _windows_overlap(a: LlmContract, b: LlmContract) -> bool:
    a_from, a_to = a.valid_from or date.min, a.valid_to or date.max
    b_from, b_to = b.valid_from or date.min, b.valid_to or date.max
    return a_from <= b_to and b_from <= a_to


def _scopes_overlap(a: LlmContract, b: LlmContract) -> bool:
    """Deux contrats de famille, ou deux contrats restreints partageant un modèle.
    Un contrat restreint et un contrat de famille coexistent (le restreint prime)."""
    a_ids, b_ids = set(a.model_ids or []), set(b.model_ids or [])
    if not a_ids and not b_ids:
        return True
    return bool(a_ids & b_ids)


STATUS_LABELS = {
    "active": "en vigueur", "upcoming": "à venir", "expired": "expiré",
    "exhausted": "plafond atteint", "inactive": "désactivé",
}


def status(contract: LlmContract, used: Decimal, day: Optional[date] = None) -> str:
    """État d'affichage : inactive | upcoming | expired | exhausted | active."""
    day = day or date.today()
    if not contract.is_active:
        return "inactive"
    if contract.valid_from is not None and day < contract.valid_from:
        return "upcoming"
    if contract.valid_to is not None and day > contract.valid_to:
        return "expired"
    if contract.cap_eur is not None and used >= contract.cap_eur:
        return "exhausted"
    return "active"


def window_label(contract: LlmContract) -> str:
    if contract.valid_from and contract.valid_to:
        return f"du {contract.valid_from:%d/%m/%Y} au {contract.valid_to:%d/%m/%Y}"
    if contract.valid_from:
        return f"à partir du {contract.valid_from:%d/%m/%Y}"
    if contract.valid_to:
        return f"jusqu'au {contract.valid_to:%d/%m/%Y}"
    return "sans limite de durée"


# ---------------------------------------------------------------------
# Lecture
# ---------------------------------------------------------------------
def get(session: Session, contract_id: int) -> Optional[LlmContract]:
    return session.get(LlmContract, contract_id)


def get_owned(session: Session, org: Organization, contract_id: int) -> LlmContract:
    c = get(session, contract_id)
    if c is None or c.organization_id != org.id:
        raise ContractError("Contrat introuvable pour cette entité.", 404)
    return c


def list_own(session: Session, org_id: int) -> list[LlmContract]:
    return list(session.execute(
        select(LlmContract).where(LlmContract.organization_id == org_id)
        .order_by(LlmContract.family, LlmContract.is_active.desc(), LlmContract.id)
    ).scalars())


def list_inherited(session: Session, org: Organization) -> list[tuple[LlmContract, Organization]]:
    """Contrats actifs posés par les ancêtres, du plus proche au plus lointain."""
    out: list[tuple[LlmContract, Organization]] = []
    for node in hierarchy.chain(session, org)[1:]:
        out.extend((c, node) for c in list_own(session, node.id) if c.is_active)
    return out


def spent(session: Session, contract_id: int) -> Decimal:
    """Dépense totale imputée au contrat (le plafond porte sur toute sa durée)."""
    return Decimal(session.execute(
        select(func.coalesce(func.sum(UsageRecord.cost_eur), 0)).where(UsageRecord.contract_id == contract_id)
    ).scalar_one())


def spent_by_contract(session: Session, contract_ids: Iterable[int]) -> dict[int, Decimal]:
    ids = list(contract_ids)
    if not ids:
        return {}
    rows = session.execute(
        select(UsageRecord.contract_id, func.sum(UsageRecord.cost_eur))
        .where(UsageRecord.contract_id.in_(ids)).group_by(UsageRecord.contract_id)
    ).all()
    out = {i: Decimal("0") for i in ids}
    out.update({cid: Decimal(total) for cid, total in rows})
    return out


def usage_count(session: Session, contract_id: int) -> int:
    return int(session.execute(
        select(func.count()).select_from(UsageRecord).where(UsageRecord.contract_id == contract_id)
    ).scalar_one())


# ---------------------------------------------------------------------
# Résolution
# ---------------------------------------------------------------------
@dataclass
class ContractResolution:
    """Contrat retenu pour (entité, modèle). `blocked` : motif si le contrat le plus
    proche est inutilisable ; `contract` vaut alors ce contrat (pour le nommer)."""
    contract: Optional[LlmContract] = None
    owner: Optional[Organization] = None
    blocked: Optional[str] = None

    @property
    def usable(self) -> bool:
        return self.blocked is None

    @property
    def contract_id(self) -> Optional[int]:
        return self.contract.id if self.contract is not None and self.blocked is None else None

    @property
    def billed_to(self) -> str:
        return "contract" if self.contract_id is not None else "platform"


def resolve(
    session: Session, org: Organization, model: Any, *, day: Optional[date] = None, check_cap: bool = True,
) -> ContractResolution:
    """Contrat applicable à un appel de `model` pour le compte de `org` (voir l'en-tête du module)."""
    family = provider_family(getattr(model, "model_name", None))
    if family is None:
        return ContractResolution()
    day = day or date.today()
    nodes = hierarchy.chain(session, org)
    rows = session.execute(
        select(LlmContract).where(
            LlmContract.organization_id.in_([n.id for n in nodes]),
            LlmContract.family == family,
            LlmContract.is_active.is_(True),
        ).order_by(LlmContract.id.desc())
    ).scalars().all()
    by_org: dict[int, list[LlmContract]] = {}
    for c in rows:
        by_org.setdefault(c.organization_id, []).append(c)
    for node in nodes:
        candidates = [c for c in by_org.get(node.id, []) if covers(c, model)]
        if not candidates:
            continue
        current = [c for c in candidates if in_window(c, day)]
        if not current:
            c = candidates[0]
            return ContractResolution(c, node, (
                f"Le contrat « {c.label} » de « {node.name} » ({FAMILY_LABELS.get(family, family)}) "
                f"n'est pas en vigueur ({window_label(c)}) : appels bloqués, pas de repli sur une autre clé."
            ))
        current.sort(key=lambda c: (not c.model_ids, -c.id))  # restreint d'abord, puis le plus récent
        chosen = current[0]
        if check_cap and chosen.cap_eur is not None and spent(session, chosen.id) >= chosen.cap_eur:
            return ContractResolution(chosen, node, (
                f"Le contrat « {chosen.label} » de « {node.name} » a atteint son plafond "
                f"({chosen.cap_eur} €) : appels bloqués, pas de repli sur une autre clé."
            ))
        return ContractResolution(chosen, node)
    return ContractResolution()


def billing_for(session: Session, org_id: Optional[int], model: Any, *, check_cap: bool = True) -> ContractResolution:
    """Imputation d'un appel (contrat ou plateforme). Un contrat bloqué n'est jamais imputé."""
    if org_id is None or model is None:
        return ContractResolution()
    org = session.get(Organization, org_id)
    if org is None:
        return ContractResolution()
    return resolve(session, org, model, check_cap=check_cap)


def credentials_for(contract: Optional[LlmContract]) -> tuple[Optional[str], Optional[str], Optional[dict]]:
    """(base_url, clé en clair, en-têtes) d'un contrat — à n'utiliser que pour construire un client."""
    if contract is None:
        return (None, None, None)
    return (contract.base_url or None, decrypt_secret(contract.api_key_encrypted), contract.extra_headers or None)


def effective_hosting(model: Any, contract: Optional[LlmContract]) -> Optional[str]:
    """Hébergement effectif d'un appel : celui du contrat s'il le déclare, sinon celui du modèle."""
    if contract is not None and contract.hosting:
        return contract.hosting
    return getattr(model, "hosting", None)


def effective_sovereign(model: Any, contract: Optional[LlmContract]) -> bool:
    return bool(getattr(model, "is_sovereign", False) or (contract is not None and contract.sovereign))


# ---------------------------------------------------------------------
# Écriture
# ---------------------------------------------------------------------
def parse_headers(raw: Any) -> Optional[dict[str, str]]:
    """En-têtes HTTP : objet JSON (ou dict) à valeurs texte ; vide → None."""
    if raw is None or raw == "" or raw == {}:
        return None
    obj = raw
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ContractError(f"En-têtes HTTP : JSON invalide ({exc}).")
    if not isinstance(obj, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in obj.items()):
        raise ContractError('En-têtes HTTP : objet JSON attendu, valeurs texte (ex. {"X-Api-Version": "2"}).')
    return obj or None


def parse_cap(raw: Any) -> Optional[Decimal]:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        cap = Decimal(str(raw).replace(",", ".").strip())
    except InvalidOperation:
        raise ContractError("Plafond invalide : montant en euros attendu.")
    if cap < 0:
        raise ContractError("Le plafond ne peut pas être négatif.")
    return cap.quantize(Decimal("0.01"))


def parse_day(raw: Any) -> Optional[date]:
    if raw is None or raw == "":
        return None
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw).strip())
    except ValueError:
        raise ContractError(f"Date invalide : {raw!r} (AAAA-MM-JJ attendu).")


def _validate(session: Session, c: LlmContract) -> None:
    with session.no_autoflush:
        _validate_unflushed(session, c)


def _validate_unflushed(session: Session, c: LlmContract) -> None:
    if c.family not in FAMILIES:
        raise ContractError(f"Famille inconnue : {c.family!r} (attendu : {', '.join(FAMILIES)}).")
    if not (c.label or "").strip():
        raise ContractError("Le libellé du contrat est obligatoire.")
    if c.family == "generic" and not c.base_url:
        raise ContractError("Un endpoint compatible OpenAI exige une URL de base.")
    if c.hosting is not None and c.hosting not in HOSTINGS:
        raise ContractError(f"Hébergement invalide : {c.hosting!r}.")
    if c.valid_from and c.valid_to and c.valid_to < c.valid_from:
        raise ContractError("La fin de validité précède son début.")
    ids = sorted({int(i) for i in (c.model_ids or [])})
    if ids:
        models = session.execute(select(Model).where(Model.model_id.in_(ids))).scalars().all()
        found = {m.model_id: m for m in models}
        bad = [i for i in ids if i not in found or provider_family(found[i].model_name) != c.family]
        if bad:
            raise ContractError(f"Modèles inconnus ou hors de la famille {c.family!r} : {bad}")
    c.model_ids = ids
    if not c.is_active:
        return
    others = session.execute(
        select(LlmContract).where(
            LlmContract.organization_id == c.organization_id, LlmContract.family == c.family,
            LlmContract.is_active.is_(True), LlmContract.id != (c.id or -1),
        )
    ).scalars().all()
    for o in others:
        if _windows_overlap(c, o) and _scopes_overlap(c, o):
            raise ContractError(
                f"Chevauchement avec le contrat actif « {o.label} » ({window_label(o)}) sur le même périmètre "
                "de modèles : désactive-le ou ajuste les dates.", 409,
            )


def create(
    session: Session, org: Organization, *, family: str, label: str, reference: Optional[str] = None,
    base_url: Optional[str] = None, api_key: Optional[str] = None, extra_headers: Any = None,
    model_ids: Iterable[int] = (), valid_from: Any = None, valid_to: Any = None, cap_eur: Any = None,
    hosting: Optional[str] = None, sovereign: bool = False, is_active: bool = True, created_by: Optional[int] = None,
) -> LlmContract:
    c = LlmContract(
        organization_id=org.id, family=(family or "").strip().lower(), label=(label or "").strip(),
        reference=(reference or "").strip() or None, base_url=(base_url or "").strip() or None,
        api_key_encrypted=encrypt_secret(api_key) if api_key else None,
        extra_headers=parse_headers(extra_headers), model_ids=list(model_ids or []),
        valid_from=parse_day(valid_from), valid_to=parse_day(valid_to), cap_eur=parse_cap(cap_eur),
        hosting=hosting or None, sovereign=bool(sovereign), is_active=bool(is_active), created_by=created_by,
    )
    _validate(session, c)
    session.add(c)
    session.commit()
    return c


_UPDATABLE = ("label", "reference", "base_url", "extra_headers", "model_ids", "valid_from", "valid_to",
              "cap_eur", "hosting", "sovereign", "is_active")


def update(
    session: Session, contract: LlmContract, *, api_key: Optional[str] = None, clear_api_key: bool = False,
    **fields: Any,
) -> LlmContract:
    """Met à jour les champs fournis. La famille ne change pas (créer un autre contrat).
    `api_key` None = clé inchangée ; `clear_api_key` l'efface."""
    unknown = set(fields) - set(_UPDATABLE)
    if unknown:
        raise ContractError(f"Champs non modifiables : {sorted(unknown)}")
    parsers = {
        "label": lambda v: (v or "").strip(), "reference": lambda v: (v or "").strip() or None,
        "base_url": lambda v: (v or "").strip() or None, "extra_headers": parse_headers,
        "model_ids": lambda v: list(v or []), "valid_from": parse_day, "valid_to": parse_day,
        "cap_eur": parse_cap, "hosting": lambda v: v or None, "sovereign": bool, "is_active": bool,
    }
    before = {k: getattr(contract, k) for k in _UPDATABLE}
    try:
        for k, v in fields.items():
            setattr(contract, k, parsers[k](v))
        _validate(session, contract)
    except ContractError:
        for k, v in before.items():
            setattr(contract, k, v)
        raise
    if clear_api_key:
        contract.api_key_encrypted = None
    elif api_key:
        contract.api_key_encrypted = encrypt_secret(api_key)
    session.commit()
    return contract


def delete(session: Session, contract: LlmContract) -> None:
    """Supprime un contrat jamais utilisé ; un contrat imputé est désactivé, pas supprimé."""
    if usage_count(session, contract.id):
        raise ContractError("Ce contrat porte déjà de la consommation : désactive-le au lieu de le supprimer.", 409)
    session.delete(contract)
    session.commit()
