"""
Couche d'accès aux données pour l'UI web.

Retourne des dicts prêts pour les templates (scores convertis en float ou None)
afin de garder les templates Jinja simples.

Isolation par organisation (ADR-077) : les DAO manipulant des données
métier prennent `org_id` en premier argument obligatoire. Les entités
globales — catalogue de modèles, prompts d'évaluation, types — n'ont pas de
`organization_id` (partagées entre orgs).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from models import (
    EvaluationPrompt,
    Model,
    PromptType,
    RunEvaluation,
    RunResult,
    RunRow,
    ScheduledRun,
    Test,
)


def _f(x: Any) -> Optional[float]:
    """Decimal|None -> float|None (pour l'affichage)."""
    if x is None:
        return None
    if isinstance(x, Decimal):
        return float(x)
    return float(x)


# -----------------------------
# Tableau de bord / runs
# -----------------------------
def leaderboard(session: Session, org_id: int) -> list[dict[str, Any]]:
    stmt = (
        select(
            Model.model_id,
            Model.model_name,
            Model.model_version,
            func.count(func.distinct(RunRow.run_id)).label("n_runs"),
            func.count(RunEvaluation.test_id).label("n_evals"),
            func.avg(RunEvaluation.response_quality_score).label("avg_response"),
            func.avg(RunEvaluation.citation_quality_score).label("avg_citation"),
        )
        .join(RunRow, RunRow.tested_model_id == Model.model_id)
        .join(RunEvaluation, RunEvaluation.run_id == RunRow.run_id)
        .where(RunRow.organization_id == org_id)
        .group_by(Model.model_id, Model.model_name, Model.model_version)
        .order_by(func.avg(RunEvaluation.response_quality_score).desc().nullslast())
    )
    out = []
    for r in session.execute(stmt).all():
        out.append(
            dict(
                model_id=r.model_id,
                model_name=r.model_name,
                model_version=r.model_version,
                n_runs=r.n_runs,
                n_evals=r.n_evals,
                avg_response=_f(r.avg_response),
                avg_citation=_f(r.avg_citation),
            )
        )
    return out


def list_runs(session: Session, org_id: int) -> list[dict[str, Any]]:
    stmt = (
        select(
            RunRow.run_id,
            RunRow.started_at,
            RunRow.run_meta,
            Model.model_name,
            Model.model_version,
            func.count(RunEvaluation.test_id).label("n_evals"),
            func.avg(RunEvaluation.response_quality_score).label("avg_response"),
            func.avg(RunEvaluation.citation_quality_score).label("avg_citation"),
        )
        .join(Model, Model.model_id == RunRow.tested_model_id)
        .outerjoin(RunEvaluation, RunEvaluation.run_id == RunRow.run_id)
        .where(RunRow.organization_id == org_id)
        .group_by(
            RunRow.run_id,
            RunRow.started_at,
            RunRow.run_meta,
            Model.model_name,
            Model.model_version,
        )
        .order_by(RunRow.run_id.desc())
    )
    out = []
    for r in session.execute(stmt).all():
        out.append(
            dict(
                run_id=r.run_id,
                started_at=r.started_at,
                run_meta=r.run_meta,
                model_name=r.model_name,
                model_version=r.model_version,
                n_evals=r.n_evals,
                avg_response=_f(r.avg_response),
                avg_citation=_f(r.avg_citation),
            )
        )
    return out


def get_run_detail(session: Session, org_id: int, run_id: int) -> Optional[dict[str, Any]]:
    run = session.execute(
        select(RunRow, Model)
        .join(Model, Model.model_id == RunRow.tested_model_id)
        .where(RunRow.run_id == run_id, RunRow.organization_id == org_id)
    ).first()
    if run is None:
        return None
    run_row, tested_model = run

    result_rows = session.execute(
        select(RunResult, Test)
        .join(Test, Test.test_id == RunResult.test_id)
        .where(RunResult.run_id == run_id)
        .order_by(RunResult.test_id)
    ).all()

    eval_rows = session.execute(
        select(RunEvaluation, Model.model_version)
        .join(Model, Model.model_id == RunEvaluation.judge_model_id)
        .where(RunEvaluation.run_id == run_id)
        .order_by(RunEvaluation.test_id, RunEvaluation.judge_model_id, RunEvaluation.judge_run_index)
    ).all()

    evals_by_test: dict[int, list[dict[str, Any]]] = {}
    for ev, judge_version in eval_rows:
        evals_by_test.setdefault(ev.test_id, []).append(
            dict(
                judge_version=judge_version,
                judge_run_index=ev.judge_run_index,
                response_score=_f(ev.response_quality_score),
                response_label=ev.response_quality_label,
                citation_score=_f(ev.citation_quality_score),
                citation_label=ev.citation_quality_label,
            )
        )

    results = []
    for rr, test in result_rows:
        results.append(
            dict(
                test_id=test.test_id,
                prompt=test.prompt,
                expected_answer=test.expected_answer,
                raw_answer=rr.raw_answer,
                raw_citations=rr.raw_citations or [],
                evals=evals_by_test.get(test.test_id, []),
            )
        )

    return dict(
        run_id=run_row.run_id,
        started_at=run_row.started_at,
        run_meta=run_row.run_meta,
        model_name=tested_model.model_name,
        model_version=tested_model.model_version,
        results=results,
    )


# -----------------------------
# Modèles (catalogue global — pas de filtre par org)
# -----------------------------
def list_models(session: Session, active_only: bool = True) -> list[Model]:
    stmt = select(Model).order_by(Model.model_id)
    if active_only:
        stmt = stmt.where(Model.is_active.is_(True))
    return list(session.execute(stmt).scalars().all())


def get_model(session: Session, model_id: int) -> Optional[Model]:
    return session.get(Model, model_id)


def model_run_refs(session: Session, model_id: int) -> int:
    """Nombre de références au modèle dans l'historique (runs testés + évaluations juge)."""
    n_runs = session.execute(
        select(func.count()).select_from(RunRow).where(RunRow.tested_model_id == model_id)
    ).scalar_one()
    n_evals = session.execute(
        select(func.count()).select_from(RunEvaluation).where(RunEvaluation.judge_model_id == model_id)
    ).scalar_one()
    return int(n_runs) + int(n_evals)


def create_model(
    session: Session,
    *,
    model_name: str,
    model_version: str,
    base_url: Optional[str],
    api_key: Optional[str],
    extra_headers: Optional[dict[str, Any]],
    search_config: Optional[dict[str, Any]] = None,
) -> Model:
    model = Model(
        model_name=model_name,
        model_version=model_version,
        base_url=base_url or None,
        api_key=api_key or None,
        extra_headers=extra_headers or None,
        search_config=search_config or None,
        is_active=True,
    )
    session.add(model)
    session.commit()
    return model


def update_model(
    session: Session,
    model_id: int,
    *,
    model_name: str,
    model_version: str,
    base_url: Optional[str],
    api_key: Optional[str],       # None = inchangée ; "" via clear_api_key
    clear_api_key: bool,
    extra_headers: Optional[dict[str, Any]],
    search_config: Optional[dict[str, Any]] = None,
) -> Model:
    model = session.get(Model, model_id)
    if model is None:
        raise ValueError(f"model_id={model_id} introuvable")
    model.model_name = model_name
    model.model_version = model_version
    model.base_url = base_url or None
    model.extra_headers = extra_headers or None
    model.search_config = search_config or None
    if clear_api_key:
        model.api_key = None
    elif api_key:  # champ laissé vide = clé existante conservée
        model.api_key = api_key
    session.commit()
    return model


def set_model_active(session: Session, model_id: int, active: bool) -> None:
    model = session.get(Model, model_id)
    if model is None:
        raise ValueError(f"model_id={model_id} introuvable")
    model.is_active = active
    session.commit()


def toggle_model_judge(session: Session, model_id: int) -> bool:
    """Inverse le flag « juge » et renvoie la nouvelle valeur."""
    model = session.get(Model, model_id)
    if model is None:
        raise ValueError(f"model_id={model_id} introuvable")
    model.is_judge = not model.is_judge
    session.commit()
    return model.is_judge


def delete_model(session: Session, model_id: int) -> None:
    """Suppression réelle, refusée si l'historique y fait référence."""
    if model_run_refs(session, model_id):
        raise ValueError(
            "Ce modèle est référencé par des runs ou des évaluations : "
            "désactive-le plutôt (l'historique doit rester intact)."
        )
    model = session.get(Model, model_id)
    if model is None:
        raise ValueError(f"model_id={model_id} introuvable")
    session.delete(model)
    session.commit()


# -----------------------------
# Runs programmés
# -----------------------------
def list_schedules(
    session: Session, org_id: int, perimeter_id: Optional[int] = None
) -> list[ScheduledRun]:
    stmt = (
        select(ScheduledRun)
        .where(ScheduledRun.organization_id == org_id)
        .order_by(ScheduledRun.schedule_id)
    )
    if perimeter_id is not None:
        stmt = stmt.where(ScheduledRun.perimeter_id == perimeter_id)
    return list(session.execute(stmt).scalars().all())


def get_schedule(
    session: Session, org_id: int, schedule_id: int
) -> Optional[ScheduledRun]:
    sr = session.get(ScheduledRun, schedule_id)
    if sr is None or sr.organization_id != org_id:
        return None
    return sr


def create_schedule(
    session: Session,
    org_id: int,
    *,
    perimeter_id: int,
    name: str,
    tested_models: list[str],
    judges: list[dict[str, Any]],
    test_ids: Optional[list[int]],
    note: Optional[str],
    schedule_kind: str,
    schedule_config: dict[str, Any],
    next_run_at: datetime,
) -> ScheduledRun:
    sr = ScheduledRun(
        organization_id=org_id,
        perimeter_id=perimeter_id,
        name=name,
        tested_models=tested_models,
        judges=judges,
        test_ids=test_ids,
        note=note,
        schedule_kind=schedule_kind,
        schedule_config=schedule_config,
        enabled=True,
        next_run_at=next_run_at,
    )
    session.add(sr)
    session.commit()
    return sr


def set_schedule_enabled(
    session: Session,
    org_id: int,
    schedule_id: int,
    enabled: bool,
    next_run_at: Optional[datetime] = None,
) -> None:
    sr = get_schedule(session, org_id, schedule_id)
    if sr is None:
        raise ValueError(f"schedule_id={schedule_id} introuvable pour org={org_id}")
    sr.enabled = enabled
    if enabled and next_run_at is not None:
        sr.next_run_at = next_run_at
    session.commit()


def delete_schedule(session: Session, org_id: int, schedule_id: int) -> None:
    sr = get_schedule(session, org_id, schedule_id)
    if sr is None:
        raise ValueError(f"schedule_id={schedule_id} introuvable pour org={org_id}")
    session.delete(sr)
    session.commit()


# -----------------------------
# Tests
# -----------------------------
def list_tests(
    session: Session, org_id: int, perimeter_id: Optional[int] = None
) -> list[Test]:
    stmt = select(Test).where(Test.organization_id == org_id).order_by(Test.test_id)
    if perimeter_id is not None:
        stmt = stmt.where(Test.perimeter_id == perimeter_id)
    return list(session.execute(stmt).scalars().all())


def get_test(session: Session, org_id: int, test_id: int) -> Optional[Test]:
    test = session.get(Test, test_id)
    if test is None or test.organization_id != org_id:
        return None
    return test


# prompt_types de la seed : 1 = response_quality, 2 = citation_quality.
_PROMPT_TYPE_RESPONSE = 1
_PROMPT_TYPE_CITATION = 2


def default_prompt_ids(session: Session) -> tuple[Optional[int], Optional[int]]:
    """Grilles de notation par défaut : première grille de chaque type.

    L'évaluation exige les deux grilles (INNER JOIN dans evaluate_run) — une
    question sans grille passerait la phase RUN puis ferait planter le job.
    """
    resp = session.execute(
        select(func.min(EvaluationPrompt.prompt_id)).where(
            EvaluationPrompt.prompt_type_id == _PROMPT_TYPE_RESPONSE
        )
    ).scalar()
    cit = session.execute(
        select(func.min(EvaluationPrompt.prompt_id)).where(
            EvaluationPrompt.prompt_type_id == _PROMPT_TYPE_CITATION
        )
    ).scalar()
    return resp, cit


def create_test(
    session: Session,
    org_id: int,
    *,
    perimeter_id: int,
    prompt: str,
    expected_answer: Optional[str],
    response_quality_prompt_id: Optional[int],
    citation_quality_prompt_id: Optional[int],
) -> Test:
    default_resp, default_cit = default_prompt_ids(session)
    test = Test(
        organization_id=org_id,
        perimeter_id=perimeter_id,
        prompt=prompt,
        expected_answer=expected_answer or None,
        response_quality_prompt_id=response_quality_prompt_id or default_resp,
        citation_quality_prompt_id=citation_quality_prompt_id or default_cit,
        validity_start_at=datetime.now(timezone.utc),
        validity_end_at=None,
    )
    session.add(test)
    session.commit()
    return test


def update_test(
    session: Session,
    org_id: int,
    test_id: int,
    *,
    prompt: str,
    expected_answer: Optional[str],
    response_quality_prompt_id: Optional[int],
    citation_quality_prompt_id: Optional[int],
) -> Test:
    test = get_test(session, org_id, test_id)
    if test is None:
        raise ValueError(f"test_id={test_id} introuvable pour org={org_id}")
    default_resp, default_cit = default_prompt_ids(session)
    test.prompt = prompt
    test.expected_answer = expected_answer or None
    test.response_quality_prompt_id = response_quality_prompt_id or default_resp
    test.citation_quality_prompt_id = citation_quality_prompt_id or default_cit
    session.commit()
    return test


def deactivate_test(session: Session, org_id: int, test_id: int) -> None:
    test = get_test(session, org_id, test_id)
    if test is None:
        raise ValueError(f"test_id={test_id} introuvable pour org={org_id}")
    test.validity_end_at = datetime.now(timezone.utc)
    session.commit()


def reactivate_test(session: Session, org_id: int, test_id: int) -> None:
    test = get_test(session, org_id, test_id)
    if test is None:
        raise ValueError(f"test_id={test_id} introuvable pour org={org_id}")
    test.validity_end_at = None
    session.commit()


# -----------------------------
# Prompts d'évaluation (catalogue global)
# -----------------------------
def list_prompts(session: Session) -> list[dict[str, Any]]:
    stmt = (
        select(EvaluationPrompt, PromptType.prompt_type_label)
        .join(PromptType, PromptType.prompt_type_id == EvaluationPrompt.prompt_type_id)
        .order_by(EvaluationPrompt.prompt_id)
    )
    out = []
    for prompt, type_label in session.execute(stmt).all():
        out.append(
            dict(
                prompt_id=prompt.prompt_id,
                prompt_type_id=prompt.prompt_type_id,
                prompt_type_label=type_label,
                prompt_name=prompt.prompt_name,
                prompt_text=prompt.prompt_text,
            )
        )
    return out


def get_prompt(session: Session, prompt_id: int) -> Optional[EvaluationPrompt]:
    return session.get(EvaluationPrompt, prompt_id)


def list_prompt_types(session: Session) -> list[PromptType]:
    return list(
        session.execute(select(PromptType).order_by(PromptType.prompt_type_id)).scalars().all()
    )


def create_prompt(
    session: Session, *, prompt_type_id: int, prompt_name: str, prompt_text: str
) -> EvaluationPrompt:
    prompt = EvaluationPrompt(
        prompt_type_id=prompt_type_id,
        prompt_name=prompt_name,
        prompt_text=prompt_text,
    )
    session.add(prompt)
    session.commit()
    return prompt


def update_prompt(
    session: Session,
    prompt_id: int,
    *,
    prompt_type_id: int,
    prompt_name: str,
    prompt_text: str,
) -> EvaluationPrompt:
    prompt = session.get(EvaluationPrompt, prompt_id)
    if prompt is None:
        raise ValueError(f"prompt_id={prompt_id} introuvable")
    prompt.prompt_type_id = prompt_type_id
    prompt.prompt_name = prompt_name
    prompt.prompt_text = prompt_text
    session.commit()
    return prompt
