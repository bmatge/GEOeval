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


# =====================================================================
# Tenancy (ADR-077) — organisations, utilisateurs, appartenances.
# Auth déléguée à l'infra VibeLab (headers gate/Authentik), zéro logique
# de login ici. Les rôles vivent dans `memberships.role` (voir ROLES).
# =====================================================================
class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    created_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    # Cache local du bit `lab-team` (dérivé de X-Gate-Groups) — non source de
    # vérité : les groupes du header priment à chaque requête.
    is_superuser_cached: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )


class Membership(Base):
    __tablename__ = "memberships"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), primary_key=True)
    role: Mapped[str] = mapped_column(Text, nullable=False)  # org_admin | editor | viewer
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


# =====================================================================
# ADR-077 §5 — invitations (PR#13). Token unique, TTL 30 j, acceptable
# uniquement par l'utilisateur qui présente l'email invité (matché sur
# X-Gate-Email).
# =====================================================================
class Invitation(Base):
    __tablename__ = "invitations"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    invited_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    token: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    accepted_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )


# =====================================================================
# ADR-077 §6 — audit log (PR#13). Trace toute action d'écriture d'un
# utilisateur sur une entité (tests, modèles, invitations, memberships).
# `entity_id` peut être NULL (créations, actions d'org).
# =====================================================================
class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    org_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("organizations.id"), nullable=True
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    meta_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)


# =====================================================================
# Domaine benchmark — tests, modèles, runs, évaluations, prompts, planif.
# Toutes les entités porteuses de données métier ont un `organization_id`
# pour l'isolation par org.
# =====================================================================
class Test(Base):
    __tablename__ = "tests"

    test_id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), nullable=False
    )
    perimeter_id: Mapped[int] = mapped_column(
        ForeignKey("perimeters.id"), nullable=False
    )

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


# =====================================================================
# PR#18 — Périmètre : objet intermédiaire entre une organisation et ses
# questions. Chaque question est rattachée à UN périmètre. Un périmètre
# est le contexte de recherche (site, thématique, propriété). Le champ
# `kind` reste optionnel pour distinguer visuellement plus tard.
# =====================================================================
class Perimeter(Base):
    __tablename__ = "perimeters"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # "site" | "topic" | NULL
    home_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)


class OrgCredential(Base):
    """Clé d'accès BYOK d'une org sur un modèle du catalogue (ADR-078 §1-2).

    L'`api_key_encrypted` est un blob Fernet (webapp/crypto.py). Résolution en
    cascade dans llm_clients.client_for_model() :
        org_credentials (BYOK) → models.api_key (plateforme) → env.
    """
    __tablename__ = "org_credentials"
    __table_args__ = (
        {"info": {"unique_org_model": ("organization_id", "model_id")}},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), nullable=False
    )
    model_id: Mapped[int] = mapped_column(
        ForeignKey("models.model_id"), nullable=False
    )
    base_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    api_key_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    extra_headers: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
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
    # Proposé (ou non) dans la colonne « Juges » des formulaires lancer/planifier.
    is_judge: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    # Juge « souverain » (ADR-079 §6) — hébergé par une infra publique (Albert).
    is_sovereign: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )


class ScheduledRun(Base):
    """Run programmé (one-shot ou récurrent), exécuté par webapp/scheduler.py."""
    __tablename__ = "scheduled_runs"

    schedule_id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), nullable=False
    )
    perimeter_id: Mapped[int] = mapped_column(
        ForeignKey("perimeters.id"), nullable=False
    )
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
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), nullable=False
    )
    perimeter_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("perimeters.id"), nullable=True
    )
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

# =====================================================================
# ADR-078 §3-5 (PR#15) — pricing versionné, usage row-par-appel, budget.
# =====================================================================
class ModelPricing(Base):
    """Prix par 1M tokens (€) — versionné par (effective_from, effective_to).

    Une seule row active par model_id à un instant t (effective_to IS NULL ou > now()).
    L'édition crée une NOUVELLE row et clôt l'ancienne (traçabilité, ADR-076).
    """
    __tablename__ = "model_pricing"

    id: Mapped[int] = mapped_column(primary_key=True)
    model_id: Mapped[int] = mapped_column(
        ForeignKey("models.model_id"), nullable=False
    )
    input_price_per_1m_tokens: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    output_price_per_1m_tokens: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False, default="EUR", server_default="EUR")
    effective_from: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    effective_to: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )


class UsageRecord(Base):
    """Consommation row-par-appel LLM. `billed_to` = 'platform' | 'byok' (ADR-078 §5)."""
    __tablename__ = "usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), nullable=False
    )
    model_id: Mapped[int] = mapped_column(
        ForeignKey("models.model_id"), nullable=False
    )
    run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("runs.run_id"), nullable=True
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # 'tested' | 'judge'
    billed_to: Mapped[str] = mapped_column(Text, nullable=False)  # 'platform' | 'byok'
    ts: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    cost_eur: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=Decimal("0"), server_default="0")
    # Coût réel provider en USD (OpenRouter, ADR-080 §6.3) — NULL si coût estimé.
    cost_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6), nullable=True)


class Budget(Base):
    """Plafond mensuel par org (€/mois). Soft-stop : refuse un nouveau scan si
    spent + estimate > cap, mais laisse aller au bout un scan en cours."""
    __tablename__ = "budgets"

    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), primary_key=True
    )
    monthly_cap_eur: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False, default="EUR", server_default="EUR")
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )
    updated_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )


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


# =====================================================================
# ADR-079 §1 (PR#16) — vérité de référence versionnée pour un test.
# Résolution par date via valid_from/valid_to (une seule row active à un
# instant t). L'édition crée une NOUVELLE version et clôt l'ancienne.
# =====================================================================
class GoldAnnotation(Base):
    """Annotation humaine d'une paire (test, run) — gold set ADR-079 §2.

    UNIQUE(test_id, run_id, annotator_email) : un même annotateur ne rate pas
    deux fois la même paire. Labels catégoriels obligatoires (vocab fixe),
    scores 0-10.
    """
    __tablename__ = "gold_annotations"

    id: Mapped[int] = mapped_column(primary_key=True)
    test_id: Mapped[int] = mapped_column(ForeignKey("tests.test_id"), nullable=False)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.run_id"), nullable=False)
    ground_truth_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    annotator_email: Mapped[str] = mapped_column(Text, nullable=False)
    response_label: Mapped[str] = mapped_column(Text, nullable=False)
    response_score: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False)
    citation_label: Mapped[str] = mapped_column(Text, nullable=False)
    citation_score: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    annotated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class TestGroundTruth(Base):
    __tablename__ = "test_ground_truth"

    id: Mapped[int] = mapped_column(primary_key=True)
    test_id: Mapped[int] = mapped_column(ForeignKey("tests.test_id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    reference_answer: Mapped[str] = mapped_column(Text, nullable=False)
    reference_urls: Mapped[Optional[list[Any]]] = mapped_column(JSONB, nullable=True)
    valid_from: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    valid_to: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
