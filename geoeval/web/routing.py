"""
Politique de routage des entités (ADR-089 §2.6, chantier E5).

Posée par entité, héritée et **restrictive uniquement** : un enfant ne relâche
jamais une contrainte de ses ancêtres.
- `allowed_families` : fournisseurs autorisés (intersection sur la chaîne ;
  NULL = pas de restriction à ce niveau). S'applique aux IA évaluées ET aux notateurs.
- `sovereign_only` / `eu_only` : vrais dès qu'un niveau les pose. S'appliquent aux
  **notateurs** seulement (arbitrage E5) : les IA évaluées sont l'objet de la mesure.
  Souveraineté et hébergement s'évaluent sur l'appel effectif : le contrat retenu
  peut les déclarer, sinon le modèle (hébergement inconnu ⇒ refusé si UE obligatoire).

La politique s'applique à tous, administrateurs compris : c'est une règle de
conformité de l'entité, pas un droit d'usage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Optional

from sqlalchemy.orm import Session

from geoeval.core.llm_clients import provider_family
from geoeval.db.models import Organization, RoutingPolicy
from geoeval.web import contracts, hierarchy


@dataclass
class EffectivePolicy:
    allowed_families: Optional[set[str]] = None          # None = tous les fournisseurs
    sovereign_only: bool = False
    eu_only: bool = False
    families_from: list[Organization] = field(default_factory=list)
    sovereign_from: Optional[Organization] = None
    eu_from: Optional[Organization] = None

    @property
    def is_restricted(self) -> bool:
        return self.allowed_families is not None or self.sovereign_only or self.eu_only


def get_own(session: Session, org_id: int) -> Optional[RoutingPolicy]:
    return session.get(RoutingPolicy, org_id)


def set_policy(
    session: Session, org: Organization, *, allowed_families: Optional[Iterable[str]],
    sovereign_only: bool, eu_only: bool, updated_by: Optional[int] = None,
) -> Optional[RoutingPolicy]:
    """Enregistre la politique propre de l'entité. Sans aucune contrainte, la ligne est
    supprimée (l'entité n'ajoute rien à ce qu'elle hérite)."""
    fams: Optional[list[str]] = None
    if allowed_families is not None:
        fams = sorted({(f or "").strip().lower() for f in allowed_families if (f or "").strip()})
        unknown = [f for f in fams if f not in contracts.FAMILIES]
        if unknown:
            raise contracts.ContractError(f"Fournisseurs inconnus : {unknown}")
    row = get_own(session, org.id)
    if fams is None and not sovereign_only and not eu_only:
        if row is not None:
            session.delete(row)
            session.commit()
        return None
    if row is None:
        row = RoutingPolicy(organization_id=org.id)
        session.add(row)
    row.allowed_families = fams
    row.sovereign_only = bool(sovereign_only)
    row.eu_only = bool(eu_only)
    row.updated_by = updated_by
    session.commit()
    return row


def effective(session: Session, org: Organization) -> EffectivePolicy:
    """Politique effective : combinaison restrictive de l'entité et de ses ancêtres."""
    eff = EffectivePolicy()
    for node in hierarchy.chain(session, org):
        p = get_own(session, node.id)
        if p is None:
            continue
        if p.allowed_families is not None:
            fams = set(p.allowed_families)
            eff.allowed_families = fams if eff.allowed_families is None else eff.allowed_families & fams
            eff.families_from.append(node)
        if p.sovereign_only and eff.sovereign_from is None:
            eff.sovereign_only, eff.sovereign_from = True, node
        if p.eu_only and eff.eu_from is None:
            eff.eu_only, eff.eu_from = True, node
    return eff


def _family_ok(policy: EffectivePolicy, model: Any) -> bool:
    return policy.allowed_families is None or provider_family(getattr(model, "model_name", None)) in policy.allowed_families


def judge_violation(session: Session, org: Organization, policy: EffectivePolicy, model: Any,
                    *, day: Optional[date] = None) -> Optional[str]:
    """Motif de refus d'un notateur au regard de la souveraineté / de l'hébergement, ou None."""
    if not (policy.sovereign_only or policy.eu_only):
        return None
    contract = contracts.resolve(session, org, model, day=day, check_cap=False).contract
    if policy.sovereign_only and not contracts.effective_sovereign(model, contract):
        return (f"Notateur {model.model_version} non souverain : « {policy.sovereign_from.name} » impose "
                "des notateurs souverains.")
    if policy.eu_only and contracts.effective_hosting(model, contract) != "eu":
        return (f"Notateur {model.model_version} non hébergé dans l'UE (ou hébergement inconnu) : "
                f"« {policy.eu_from.name} » impose un hébergement dans l'Union européenne.")
    return None


def violations(
    session: Session, org: Organization, *, tested: Iterable[Any], judges: Iterable[Any], day: Optional[date] = None,
) -> list[str]:
    """Motifs de refus d'une sélection de modèles (vide = conforme)."""
    policy = effective(session, org)
    if not policy.is_restricted:
        return []
    out: list[str] = []
    allowed = ", ".join(sorted(policy.allowed_families or [])) or "aucun"
    tested, judges = list(tested), list(judges)
    for m in tested + judges:
        if not _family_ok(policy, m):
            msg = f"Fournisseur de {m.model_version} non autorisé pour cette entité (autorisés : {allowed})."
            if msg not in out:
                out.append(msg)
    for j in judges:
        reason = judge_violation(session, org, policy, j, day=day)
        if reason:
            out.append(reason)
    return out


def filter_models(session: Session, org: Organization, models: list[Any], *, as_judges: bool) -> list[Any]:
    """Modèles proposables dans les formulaires (même règle que `violations`)."""
    policy = effective(session, org)
    if not policy.is_restricted:
        return models
    kept = [m for m in models if _family_ok(policy, m)]
    if as_judges:
        kept = [m for m in kept if judge_violation(session, org, policy, m) is None]
    return kept
