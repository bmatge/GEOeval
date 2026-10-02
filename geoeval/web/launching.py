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

from typing import Any, Optional

from sqlalchemy.orm import Session

from geoeval.core.load import load_tests
from geoeval.db.models import Job, Model, ScheduledRun
from geoeval.web import budget, org_models, perimeters, pricing, services
from geoeval.worker import jobs as jobqueue

# Providers utilisables comme modèle TESTÉ (dispatch de run.py, avec recherche web).
TESTABLE_PROVIDERS = {"openai", "chatgpt", "gpt", "mistral", "mistralai", "gemini", "google", "openrouter"}

# Statut HTTP suggéré par type d'erreur (l'UI et l'API s'y conforment).
KIND_STATUS = {"validation": 400, "forbidden_models": 403, "budget": 402, "not_found": 404}


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
    """True si le rôle échappe à l'allowlist org_models : org_admin de l'org
    ou admin plateforme voient tout le catalogue actif."""
    return is_platform_admin or role == "org_admin"


def allowed_models(session: Session, org_id: int, *, role: Optional[str], is_platform_admin: bool) -> list[Model]:
    """Catalogue actif, restreint à l'allowlist de l'org pour editor/viewer."""
    models = services.list_models(session)
    if is_unrestricted(role, is_platform_admin):
        return models
    return org_models.filter_models(session, org_id, models)


def allowed_model_versions(
    session: Session, org_id: int, *, role: Optional[str], is_platform_admin: bool
) -> Optional[set[str]]:
    """`model_version` autorisées pour une soumission ; None = tout.

    Garde-fou serveur : le filtrage du formulaire ne suffit pas, une requête
    forgée (UI ou API) doit aussi être refusée pour un editor/viewer.
    """
    if is_unrestricted(role, is_platform_admin):
        return None
    return {m.model_version for m in allowed_models(session, org_id, role=role, is_platform_admin=is_platform_admin)}


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
    peri_tests = [t for t in load_tests(session, organization_id=org_id) if t.perimeter_id == perimeter_id]
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
def estimate_and_check_budget(session: Session, org_id: int, params: dict[str, Any]) -> dict[str, Any]:
    """Devis prévisionnel puis contrôle des plafonds (mois, jour). Renvoie
    l'estimation ; lève LaunchError('budget') si un plafond serait dépassé."""
    tests_for_estimate = load_tests(
        session, test_ids=params["test_ids"], active_only=True, ready_only=True, organization_id=org_id,
    )
    estimate = pricing.estimate_scan_cost(
        session, org_id=org_id, tests=tests_for_estimate,
        tested_models=params["tested_models"], judges=params["judges"],
    )
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
    params["estimate"] = estimate_and_check_budget(session, org_id, params)
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
    estimate_and_check_budget(session, org_id, params)
    return jobqueue.submit(session, dict(
        **params,
        organization_id=org_id,
        perimeter_id=schedule.perimeter_id,
        note=schedule.note or f"planifié : {schedule.name} (manuel)",
    ))
