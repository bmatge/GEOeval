"""
Règles de lancement d'une évaluation (ADR-088 §2.3 — règles dans les services).

Ce module est l'unique porte d'entrée pour lancer ou planifier un run, que
l'appelant soit l'UI HTML ou l'API v1 : liste blanche des modèles de
l'organisation (EPIC-001 S4.2), validation de la sélection dans le périmètre,
devis prévisionnel et plafond budgétaire (ADR-078 / ADR-080), mise en file.

Les erreurs sont typées (`LaunchError.kind`) ; chaque couche de présentation
les traduit (HTTPException pour l'UI, problem+json pour l'API).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from sqlalchemy.orm import Session

from geoeval.core.load import load_tests
from geoeval.db.models import Job, Model, Organization, Perimeter, ScheduledRun, Test
from geoeval.web import budget, contracts, org_models, perimeters, pools, pricing, routing, services
from geoeval.worker import jobs as jobqueue

# Providers utilisables comme modèle TESTÉ (dispatch de run.py, avec recherche web).
TESTABLE_PROVIDERS = {"openai", "chatgpt", "gpt", "mistral", "mistralai", "gemini", "google", "openrouter"}

# Statut HTTP suggéré par type d'erreur (l'UI et l'API s'y conforment).
KIND_STATUS = {"validation": 400, "forbidden_models": 403, "routing": 403, "budget": 402, "contract": 409,
               "not_found": 404}

# Refus qui font sauter (et tracer) une échéance programmée au lieu de l'exécuter.
SKIPPABLE_KINDS = ("budget", "routing", "contract")


class LaunchError(Exception):
    """Refus de lancement, typé pour la couche de présentation."""

    def __init__(self, kind: str, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail
        self.extra = extra

    @property
    def status(self) -> int:
        return KIND_STATUS.get(self.kind, 400)


# ---------------------------------------------------------------------
# Liste blanche des modèles (EPIC-001 S4.2)
# ---------------------------------------------------------------------
def is_unrestricted(role: Optional[str], is_platform_admin: bool) -> bool:
    """True si le rôle échappe à la liste blanche de son périmètre : org_admin
    (qui gère les listes de son sous-arbre) ou admin plateforme. L'org_admin reste
    borné par les listes posées au-dessus de son entité d'ancrage (ADR-089 §2.2)."""
    return is_platform_admin or role == "org_admin"


def _ignore_from_depth(session: Session, org_id: int, role: Optional[str]) -> Optional[int]:
    """Profondeur à partir de laquelle les listes blanches ne s'appliquent plus à cet acteur.

    editor / viewer : aucune (toutes les listes de la chaîne s'appliquent).
    org_admin : profondeur de son ancre (E2) — il gère les listes de son
    sous-arbre ; à défaut d'ancre connue, l'entité elle-même (comportement E1).
    """
    if not is_unrestricted(role, False):
        return None
    depth = getattr(role, "anchor_depth", None)
    if depth is None:
        org = session.get(Organization, org_id)
        depth = org.depth if org is not None else 0
    return depth


def allowed_models(session: Session, org_id: int, *, role: Optional[str], is_platform_admin: bool) -> list[Model]:
    """Catalogue actif restreint par la liste blanche effective (ADR-089 §2.2).

    Admin plateforme : tout. org_admin : listes au-dessus de son ancre seulement.
    editor / viewer : listes de l'entité et de tous ses ancêtres.
    """
    models = services.list_models(session)
    if is_platform_admin:
        return models
    return org_models.filter_models(session, org_id, models, ignore_from_depth=_ignore_from_depth(session, org_id, role))


def allowed_model_versions(
    session: Session, org_id: int, *, role: Optional[str], is_platform_admin: bool
) -> Optional[set[str]]:
    """`model_version` autorisées pour une soumission ; None = tout le catalogue actif.

    Garde-fou serveur : le filtrage du formulaire ne suffit pas, une requête
    forgée (UI ou API) doit aussi être refusée. Mêmes règles que `allowed_models`.
    """
    if is_platform_admin:
        return None
    allowed_ids = org_models.effective_allowed_ids(
        session, org_id, ignore_from_depth=_ignore_from_depth(session, org_id, role),
    )
    if allowed_ids is None:
        return None
    return {m.model_version for m in services.list_models(session) if m.model_id in allowed_ids}


# ---------------------------------------------------------------------
# Questions d'un run
# ---------------------------------------------------------------------
def tests_for_run(
    session: Session, org_id: int, *, perimeter_id: Optional[int], test_ids: Optional[list[int]] = None,
) -> list[Test]:
    """Questions actives et prêtes qu'un run exécuterait. Avec un périmètre (E4) :
    ses questions propres + celles des pools abonnés (visibles), éventuellement
    restreintes à `test_ids`. Sans périmètre (runs historiques) : questions de l'org."""
    if perimeter_id is not None:
        peri = session.get(Perimeter, int(perimeter_id))
        if peri is None or peri.organization_id != org_id:
            return []
        return pools.effective_tests(session, peri, test_ids=test_ids or None)
    return list(load_tests(
        session, test_ids=test_ids or None, active_only=True, ready_only=True, organization_id=org_id,
    ))


# ---------------------------------------------------------------------
# Validation de la sélection
# ---------------------------------------------------------------------
def validate_selection(
    session: Session,
    org_id: int,
    *,
    perimeter_id: int,
    tested_models: list[str],
    judge_models: list[str],
    repeats: int,
    test_ids: list[int],
    role: Optional[str],
    is_platform_admin: bool,
) -> dict[str, Any]:
    """Valide modèles, juges et questions dans le périmètre. Renvoie les
    paramètres normalisés d'un run : tested_models, judges, test_ids."""
    if not tested_models or not judge_models:
        raise LaunchError("validation", "Sélectionne au moins une IA évaluée et un notateur.")
    allowed = allowed_model_versions(session, org_id, role=role, is_platform_admin=is_platform_admin)
    if allowed is not None:
        forbidden = (set(tested_models) | set(judge_models)) - allowed
        if forbidden:
            raise LaunchError(
                "forbidden_models",
                f"Modèles non autorisés pour cette organisation : {sorted(forbidden)}",
                forbidden=sorted(forbidden),
            )
    peri = perimeters.get_by_id(session, org_id, perimeter_id)
    if peri is None:
        raise LaunchError("validation", "Périmètre invalide.")
    peri_tests = tests_for_run(session, org_id, perimeter_id=perimeter_id)
    all_active_ids = [t.test_id for t in peri_tests]
    if not all_active_ids:
        raise LaunchError("validation", "Aucune question active et prête dans ce périmètre.")
    if not test_ids:
        raise LaunchError("validation", "Sélectionne au moins une question.")
    invalid = set(test_ids) - set(all_active_ids)
    if invalid:
        raise LaunchError("validation", f"Questions hors du périmètre {peri.name!r} : {sorted(invalid)}")
    selected: Optional[list[int]] = None if set(test_ids) >= set(all_active_ids) else list(test_ids)
    judges = [{"model": v, "repeats": max(1, int(repeats))} for v in judge_models]
    return dict(tested_models=list(tested_models), judges=judges, test_ids=selected)


# ---------------------------------------------------------------------
# Devis + plafond budgétaire
# ---------------------------------------------------------------------
def _models_by_version(session: Session, versions: list[str]) -> list[Model]:
    by_version = {m.model_version: m for m in services.list_models(session, active_only=False)}
    return [by_version[v] for v in versions if v in by_version]


def check_compliance(session: Session, org_id: int, params: dict[str, Any]) -> None:
    """Politique de routage et contrats (E5), avant tout devis :
    - fournisseurs autorisés (IA évaluées et notateurs), souveraineté / hébergement UE (notateurs) ;
    - contrat le plus proche de chaque modèle en vigueur et sous son plafond (sinon blocage)."""
    org = session.get(Organization, org_id)
    if org is None:
        raise LaunchError("not_found", "Organisation introuvable.")
    tested = _models_by_version(session, list(params["tested_models"]))
    judges = _models_by_version(session, [j["model"] for j in params["judges"]])
    problems = routing.violations(session, org, tested=tested, judges=judges)
    if problems:
        raise LaunchError("routing", " ".join(problems), problems=problems)
    seen: set[int] = set()
    for m in tested + judges:
        if m.model_id in seen:
            continue
        seen.add(m.model_id)
        res = contracts.resolve(session, org, m)
        if not res.usable:
            raise LaunchError("contract", res.blocked, contract_id=res.contract.id)


def _check_contract_caps(session: Session, estimate: dict[str, Any]) -> None:
    """Le devis imputé à chaque contrat plafonné doit tenir sous son plafond restant."""
    share: dict[int, Decimal] = {}
    for line in estimate.get("by_model", []):
        cid = line.get("contract_id")
        if cid is not None:
            share[cid] = share.get(cid, Decimal("0")) + Decimal(line["cost_eur"])
    for cid, amount in share.items():
        c = contracts.get(session, cid)
        if c is None or c.cap_eur is None:
            continue
        used = contracts.spent(session, cid)
        if used + amount > c.cap_eur:
            raise LaunchError(
                "contract",
                f"Le contrat « {c.label} » dépasserait son plafond : {used:.2f} € consommés + "
                f"{amount:.2f} € estimés > {c.cap_eur} €.",
                contract_id=cid, estimate_eur=str(amount),
            )


def estimate_and_check_budget(
    session: Session, org_id: int, params: dict[str, Any], *, perimeter_id: Optional[int] = None,
) -> dict[str, Any]:
    """Conformité (routage, contrats), devis prévisionnel, plafonds des contrats puis
    budget consolidé (mois, jour). Renvoie l'estimation ; lève LaunchError('routing' |
    'contract' | 'budget'). Chemin commun : lancement, « exécuter maintenant »,
    création de planification et échéance du planificateur.
    `perimeter_id` : le devis porte sur les questions effectives du périmètre (pools inclus)."""
    check_compliance(session, org_id, params)
    tests_for_estimate = tests_for_run(
        session, org_id, perimeter_id=perimeter_id, test_ids=params["test_ids"],
    )
    estimate = pricing.estimate_scan_cost(
        session, org_id=org_id, tests=tests_for_estimate,
        tested_models=params["tested_models"], judges=params["judges"],
    )
    _check_contract_caps(session, estimate)
    check = budget.check_budget(session, org_id=org_id, estimate_eur=estimate["total_eur"])
    if not check.ok:
        raise LaunchError("budget", check.reason, estimate_eur=str(estimate["total_eur"]))
    return estimate


def prepare_run(
    session: Session,
    org_id: int,
    *,
    perimeter_id: int,
    tested_models: list[str],
    judge_models: list[str],
    repeats: int,
    test_ids: list[int],
    role: Optional[str],
    is_platform_admin: bool,
) -> dict[str, Any]:
    """Validation + devis + budget. Renvoie les paramètres prêts à être mis en
    file ou enregistrés dans une planification."""
    params = validate_selection(
        session, org_id, perimeter_id=perimeter_id, tested_models=tested_models,
        judge_models=judge_models, repeats=repeats, test_ids=test_ids,
        role=role, is_platform_admin=is_platform_admin,
    )
    params["estimate"] = estimate_and_check_budget(session, org_id, params, perimeter_id=perimeter_id)
    return params


# ---------------------------------------------------------------------
# Mise en file
# ---------------------------------------------------------------------
def launch_run(
    session: Session,
    org_id: int,
    *,
    perimeter_id: int,
    tested_models: list[str],
    judge_models: list[str],
    repeats: int,
    test_ids: list[int],
    note: Optional[str],
    role: Optional[str],
    is_platform_admin: bool,
) -> Job:
    """Lance une évaluation : toutes les règles, puis mise en file."""
    params = prepare_run(
        session, org_id, perimeter_id=perimeter_id, tested_models=tested_models,
        judge_models=judge_models, repeats=repeats, test_ids=test_ids,
        role=role, is_platform_admin=is_platform_admin,
    )
    params.pop("estimate", None)
    return jobqueue.submit(session, dict(
        **params, note=note or None, organization_id=org_id, perimeter_id=perimeter_id,
    ))


def run_schedule_now(session: Session, org_id: int, schedule: ScheduledRun) -> Job:
    """Exécute immédiatement une planification. Le plafond budgétaire est
    contrôlé comme pour un lancement manuel (même règle, même service)."""
    params = dict(
        tested_models=list(schedule.tested_models),
        judges=list(schedule.judges),
        test_ids=list(schedule.test_ids) if schedule.test_ids else None,
    )
    estimate_and_check_budget(session, org_id, params, perimeter_id=schedule.perimeter_id)
    return jobqueue.submit(session, dict(
        **params,
        organization_id=org_id,
        perimeter_id=schedule.perimeter_id,
        note=schedule.note or f"planifié : {schedule.name} (manuel)",
    ))
