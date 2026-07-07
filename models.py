# models.py
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional, Any

from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import Boolean, Text, TIMESTAMP, ForeignKey, func, Numeric, Integer
from sqlalchemy.dialects.postgresql import JSONB


class Base(DeclarativeBase):
    pass


class Test(Base):
    __tablename__ = "tests"

    test_id: Mapped[int] = mapped_column(primary_key=True)

    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    expected_answer: Mapped[Optional[str]] = mapped_column(Text)

    response_quality_prompt_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("evaluation_prompts.prompt_id"), nullable=True
    )
    citation_quality_prompt_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("evaluation_prompts.prompt_id"), nullable=True
    )

    validity_start_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
    )
    validity_end_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True)
    )


class Model(Base):
    __tablename__ = "models"

    model_id: Mapped[int] = mapped_column(primary_key=True)
    model_name: Mapped[str] = mapped_column(Text, nullable=False)      # ex: "chatGPT" (provider, pilote le dispatch)
    model_version: Mapped[str] = mapped_column(Text, nullable=False)   # ex: "gpt-5.2" (id modèle API)

    # Config d'accès optionnelle (page Modèles). Vide => repli sur les
    # variables d'environnement / défauts du provider (llm_clients).
    base_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    api_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    extra_headers: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    # Désactivé = masqué des formulaires (l'historique des runs reste intact).
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")


class ScheduledRun(Base):
    """Run programmé (one-shot ou récurrent), exécuté par webapp/scheduler.py."""
    __tablename__ = "scheduled_runs"

    schedule_id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)

    tested_models: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)   # list[str] model_version
    judges: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)          # [{"model": str, "repeats": int}]
    test_ids: Mapped[Optional[list[Any]]] = mapped_column(JSONB, nullable=True)  # None = tous les tests actifs
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    schedule_kind: Mapped[str] = mapped_column(Text, nullable=False)          # once | daily | weekly | every_n_hours
    schedule_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    next_run_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_job_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class RunRow(Base):
    __tablename__ = "runs"

    run_id: Mapped[int] = mapped_column(primary_key=True)
    tested_model_id: Mapped[int] = mapped_column(ForeignKey("models.model_id"), nullable=False)

    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    run_meta: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)


class RunResult(Base):
    __tablename__ = "run_results"

    run_id: Mapped[int] = mapped_column(ForeignKey("runs.run_id"), primary_key=True)
    test_id: Mapped[int] = mapped_column(ForeignKey("tests.test_id"), primary_key=True)

    raw_answer: Mapped[str] = mapped_column(Text, nullable=False)
    raw_citations: Mapped[Optional[list[Any]]] = mapped_column(JSONB, nullable=True)


class RunEvaluation(Base):
    __tablename__ = "run_evaluations"

    run_id: Mapped[int] = mapped_column(ForeignKey("runs.run_id"), primary_key=True)
    test_id: Mapped[int] = mapped_column(ForeignKey("tests.test_id"), primary_key=True)
    judge_model_id: Mapped[int] = mapped_column(ForeignKey("models.model_id"), primary_key=True)
    judge_run_index: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)

    response_quality_label: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    response_quality_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 2), nullable=True)

    citation_quality_label: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    citation_quality_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 2), nullable=True)

class PromptType(Base):
    __tablename__ = "prompt_types"

    prompt_type_id: Mapped[int] = mapped_column(primary_key=True)
    prompt_type_label: Mapped[str] = mapped_column(Text, nullable=False)


class EvaluationPrompt(Base):
    __tablename__ = "evaluation_prompts"

    prompt_id: Mapped[int] = mapped_column(primary_key=True)
    prompt_type_id: Mapped[int] = mapped_column(ForeignKey("prompt_types.prompt_type_id"), nullable=False)
    prompt_name: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False)

