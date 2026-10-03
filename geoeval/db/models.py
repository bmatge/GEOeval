# models.py
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional, Any

from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    TIMESTAMP,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB


class Base(DeclarativeBase):
    pass


# =====================================================================
# Tenancy (ADR-077 amendée par ADR-086) — organisations, utilisateurs,
# appartenances. Auth applicative : comptes locaux (bcrypt + sessions)
# + OIDC optionnel. Les rôles d'org vivent dans `memberships.role`.
# =====================================================================
class Organization(Base):
    """Entité (ADR-089 §2.1) : nœud d'un arbre ministère > direction > service.

    `path` est le chemin matérialisé des identifiants, racine comprise, encadré
    de « / » (ex. ``/12/45/78/``) : descendants = ``path LIKE '/12/45/%'``.
    Il est calculé par geoeval.web.hierarchy, jamais saisi.
    """

    __tablename__ = "organizations"
    __table_args__ = (
        CheckConstraint("kind IN ('ministere', 'direction', 'service', 'autre')", name="ck_organizations_kind"),
        CheckConstraint("depth >= 0", name="ck_organizations_depth"),
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="ck_organizations_parent_not_self"),
        CheckConstraint("siret IS NULL OR siret ~ '^[0-9]{14}$'", name="ck_organizations_siret"),
        Index("ix_organizations_parent_id", "parent_id"),
        Index("ix_organizations_path", "path", postgresql_ops={"path": "text_pattern_ops"}),
        Index("ix_organizations_siret", "siret"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    created_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'autre'"))
    path: Mapped[str] = mapped_column(Text, nullable=False)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    siret: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class User(Base):
    __tablename__ = "users"

    __table_args__ = (
        Index("uq_users_oidc_identity", "oidc_issuer", "oidc_external_id", unique=True,
              postgresql_where=text("oidc_issuer IS NOT NULL AND oidc_external_id IS NOT NULL")),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    # Héritage ADR-077 (cache du bit `lab-team`) — conservée pour l'historique,
    # plus jamais lue depuis ADR-086. La source de vérité est is_platform_admin.
    is_superuser_cached: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # --- Auth applicative (ADR-086) ---
    # NULL = pas de mot de passe local (compte SSO-only ou jamais activé).
    password_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 'local' | 'oidc' — provider du dernier mode de création/connexion.
    auth_provider: Mapped[str] = mapped_column(
        Text, nullable=False, default="local", server_default="local"
    )
    # Identité OIDC (réconciliation anti-takeover, ADR-061/086).
    oidc_issuer: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    oidc_external_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Source de vérité du rôle plateforme (remplace la dérivation lab-team).
    is_platform_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )


class AuthToken(Base):
    """Lien one-shot de définition de mot de passe (ADR-086 §1, TTL 48 h)."""

    __tablename__ = "auth_tokens"

    __table_args__ = (Index("ix_auth_tokens_user_id", "user_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    purpose: Mapped[str] = mapped_column(
        Text, nullable=False, default="set_password", server_default="set_password"
    )
    token: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    used_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
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

    __table_args__ = (
        Index("ix_invitations_email", "email"),
        Index("ix_invitations_org_id", "org_id"),
    )
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

    __table_args__ = (
        Index("ix_audit_log_org_at", "org_id", text("at DESC")),
        Index("ix_audit_log_user_at", "user_id", text("at DESC")),
    )
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

    __table_args__ = (
        Index("ix_tests_organization_id", "organization_id"),
        Index("ix_tests_perimeter_id", "perimeter_id"),
    )
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

    __table_args__ = (
        Index("ix_perimeters_org_id", "organization_id"),
        Index("uq_perimeters_org_slug", "organization_id", "slug", unique=True),
    )
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

    L'`api_key_encrypted` est un blob Fernet (geoeval/web/crypto.py). Résolution en
    cascade dans llm_clients.client_for_model() :
        org_credentials (BYOK) → models.api_key (plateforme) → env.
    """
    __tablename__ = "org_credentials"
    __table_args__ = (
        Index("uq_org_credentials_org_model", "organization_id", "model_id", unique=True),
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


class OrgModel(Base):
    """Allowlist org ↔ modèle (EPIC-001 Phase 4, S4.1).

    Restreint les modèles du catalogue global proposés aux rôles editor/viewer
    d'une org. AUCUNE ligne pour une org = héritage du catalogue global filtré
    `models.is_active` (rétro-compat — voir geoeval/web/org_models.py).
    """
    __tablename__ = "org_models"
    __table_args__ = (
        UniqueConstraint("organization_id", "model_id", name="uq_org_models_org_model"),
        Index("ix_org_models_org_id", "organization_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), nullable=False
    )
    model_id: Mapped[int] = mapped_column(
        ForeignKey("models.model_id"), nullable=False
    )
    # TRUE = proposé aux editor/viewer ; FALSE = explicitement masqué.
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
    # Web search par modèle (ADR-080 §2.2, famille openrouter uniquement) :
    # {"engine": "native|exa|firecrawl|off", "max_results": int,
    #  "search_context_size": "low|medium|high", "allowed_domains": [...]}
    # NULL ou engine="off" = pas de recherche web.
    search_config: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    # Désactivé = masqué des formulaires (l'historique des runs reste intact).
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    # Proposé (ou non) dans la colonne « Juges » des formulaires lancer/planifier.
    is_judge: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    # Juge « souverain » (ADR-079 §6) — hébergé par une infra publique (Albert).
    is_sovereign: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )


class ScheduledRun(Base):
    """Run programmé (one-shot ou récurrent), exécuté par geoeval/worker/scheduler.py."""
    __tablename__ = "scheduled_runs"

    __table_args__ = (
        Index("ix_scheduled_runs_organization_id", "organization_id"),
        Index("ix_scheduled_runs_perimeter_id", "perimeter_id"),
    )
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
    # E3 : dernière échéance sautée par le planificateur (plafond budgétaire), avec motif.
    last_skipped_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_skip_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class RunRow(Base):
    __tablename__ = "runs"

    __table_args__ = (
        Index("ix_runs_organization_id", "organization_id"),
        Index("ix_runs_perimeter_id", "perimeter_id"),
    )
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

    __table_args__ = (
        Index("ix_model_pricing_active", "model_id", postgresql_where=text("effective_to IS NULL")),
    )
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

    __table_args__ = (
        Index("ix_usage_org_ts", "organization_id", text("ts DESC")),
        Index("ix_usage_run_id", "run_id"),
    )
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
    """Plafonds par org : mensuel (€/mois) + journalier optionnel (€/jour,
    EPIC-001 Phase 3). Soft-stop : refuse un nouveau scan si spent + estimate
    dépasse l'un des caps, mais laisse aller au bout un scan en cours."""
    __tablename__ = "budgets"

    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), primary_key=True
    )
    monthly_cap_eur: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    daily_cap_eur: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 2), nullable=True
    )
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

    __table_args__ = (
        Index("ix_gold_annotations_run_id", "run_id"),
        Index("uq_gold_annotations_test_run_annotator", "test_id", "run_id", "annotator_email", unique=True),
    )
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

    __table_args__ = (
        Index("ix_test_ground_truth_active", "test_id", postgresql_where=text("valid_to IS NULL")),
    )
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


# =====================================================================
# File de jobs persistée (ADR-088 lot 1.2) — réclamée par les processus
# worker via SELECT … FOR UPDATE SKIP LOCKED ; logs d'exécution à part.
# =====================================================================
class Job(Base):
    __tablename__ = "jobs"

    __table_args__ = (Index("ix_jobs_queue", "status", text("priority DESC"), "created_at"),)
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    phase: Mapped[str] = mapped_column(Text, nullable=False, default="")
    current: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text)
    run_ids: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    worker_id: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    claimed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))


class JobLog(Base):
    __tablename__ = "job_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ts: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    level: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)


# =====================================================================
# Jetons d'API par organisation (ADR-088 §2.3, lot 1.3b) — clients machine.
# Le jeton en clair n'est montré qu'à la création ; seule son empreinte
# SHA-256 est stockée. Le rôle porté est l'un des trois rôles d'org.
# =====================================================================
class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    prefix: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    last_used_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    revoked_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))


# =====================================================================
# Alertes budgétaires (ADR-089 §2.3 / §2.8, chantier E3) — une ligne par
# franchissement de seuil (80 / 100 %) d'un plafond, par période calendaire.
# L'unicité empêche de renvoyer la même alerte à chaque job.
# =====================================================================
class BudgetAlert(Base):
    __tablename__ = "budget_alerts"
    __table_args__ = (
        CheckConstraint("period IN ('day', 'month')", name="ck_budget_alerts_period"),
        CheckConstraint("threshold IN (80, 100)", name="ck_budget_alerts_threshold"),
        Index("uq_budget_alerts_org_period_threshold", "organization_id", "period", "period_key", "threshold", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    period: Mapped[str] = mapped_column(Text, nullable=False)
    period_key: Mapped[str] = mapped_column(Text, nullable=False)        # « 2026-10 » ou « 2026-10-03 »
    threshold: Mapped[int] = mapped_column(Integer, nullable=False)       # 80 | 100
    spent_eur: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    cap_eur: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    email_status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    emailed_to: Mapped[Optional[list[Any]]] = mapped_column(JSONB, nullable=True)
