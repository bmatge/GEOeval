"""
Couche d'accès aux données pour l'UI web.

Retourne des dicts prêts pour les templates (scores convertis en float ou None)
afin de garder les templates Jinja simples.
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
def leaderboard(session: Session) -> list[dict[str, Any]]:
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


def list_runs(session: Session) -> list[dict[str, Any]]:
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


def get_run_detail(session: Session, run_id: int) -> Optional[dict[str, Any]]:
    run = session.execute(
        select(RunRow, Model)
        .join(Model, Model.model_id == RunRow.tested_model_id)
        .where(RunRow.run_id == run_id)
    ).first()
    if run is None:
        return None
    run_row, tested_model = run

    # Résultats (réponses) du run
    result_rows = session.execute(
        select(RunResult, Test)
        .join(Test, Test.test_id == RunResult.test_id)
        .where(RunResult.run_id == run_id)
        .order_by(RunResult.test_id)
    ).all()

    # Évaluations (notes des juges), regroupées par test_id
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
# Modèles
# -----------------------------
def list_models(session: Session) -> list[Model]:
    return list(session.execute(select(Model).order_by(Model.model_id)).scalars().all())


# -----------------------------
# Tests
# -----------------------------
def list_tests(session: Session) -> list[Test]:
    return list(session.execute(select(Test).order_by(Test.test_id)).scalars().all())


def get_test(session: Session, test_id: int) -> Optional[Test]:
    return session.get(Test, test_id)


def create_test(
    session: Session,
    *,
    prompt: str,
    expected_answer: Optional[str],
    response_quality_prompt_id: Optional[int],
    citation_quality_prompt_id: Optional[int],
) -> Test:
    test = Test(
        prompt=prompt,
        expected_answer=expected_answer or None,
        response_quality_prompt_id=response_quality_prompt_id,
        citation_quality_prompt_id=citation_quality_prompt_id,
        validity_start_at=datetime.now(timezone.utc),
        validity_end_at=None,
    )
    session.add(test)
    session.commit()
    return test


def update_test(
    session: Session,
    test_id: int,
    *,
    prompt: str,
    expected_answer: Optional[str],
    response_quality_prompt_id: Optional[int],
    citation_quality_prompt_id: Optional[int],
) -> Test:
    test = session.get(Test, test_id)
    if test is None:
        raise ValueError(f"test_id={test_id} introuvable")
    test.prompt = prompt
    test.expected_answer = expected_answer or None
    test.response_quality_prompt_id = response_quality_prompt_id
    test.citation_quality_prompt_id = citation_quality_prompt_id
    session.commit()
    return test


def deactivate_test(session: Session, test_id: int) -> None:
    test = session.get(Test, test_id)
    if test is None:
        raise ValueError(f"test_id={test_id} introuvable")
    test.validity_end_at = datetime.now(timezone.utc)
    session.commit()


def reactivate_test(session: Session, test_id: int) -> None:
    test = session.get(Test, test_id)
    if test is None:
        raise ValueError(f"test_id={test_id} introuvable")
    test.validity_end_at = None
    session.commit()


# -----------------------------
# Prompts d'évaluation
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
