# models.py
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional, Any

from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
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
        CheckConstraint("status IN ('draft', 'published', 'retired')", name="ck_tests_status"),
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
    # Cycle de vie (E8) : draft (brouillon, hors runs / pools / campagnes) | published | retired.
    # Invariant : retired ⇔ validity_end_at posé (historique ADR-076 : on retire, on ne supprime pas).
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="published")


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
    # E4 : domaines officiels du site (hôtes normalisés, sans schéma), base de la
    # mesure des citations vers les sources du site.
    domains: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))


class OrgCredential(Base):
    """Ancienne clé BYOK d'une org sur un modèle (ADR-078). GELÉE depuis E5 : les
    lignes ont été converties en `llm_contracts` (révision 0005) et ne sont plus lues.
    Conservée pour un retour arrière ; à supprimer par une révision ultérieure.
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
    # Hébergement de l'endpoint plateforme (E5) : 'eu' | 'non_eu' | NULL (inconnu).
    # Un contrat peut le surcharger ; « UE obligatoire » refuse l'inconnu.
    hosting: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


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
        Index("ix_runs_campaign_id", "campaign_id"),
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
    # Campagne exécutée par ce run (E8) — NULL = run hors campagne.
    campaign_id: Mapped[Optional[int]] = mapped_column(ForeignKey("campaigns.id"), nullable=True)


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
        Index("ix_usage_contract_id", "contract_id"),
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
    # Contrat LLM imputé (E5) — NULL = clé plateforme.
    contract_id: Mapped[Optional[int]] = mapped_column(ForeignKey("llm_contracts.id"), nullable=True)


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


# =====================================================================
# Pools de questions, thèmes (ADR-089 §2.5, chantier E4)
# =====================================================================
class Theme(Base):
    """Thème du catalogue global (géré par l'administration plateforme)."""

    __tablename__ = "themes"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class QuestionPool(Base):
    """Pool de questions : propriété d'une entité, partagé par référence selon sa
    visibilité (private = l'entité seule ; descendants = l'entité et son sous-arbre ;
    all = toutes les entités). Exécuté en l'abonnant à un périmètre."""

    __tablename__ = "question_pools"
    __table_args__ = (
        CheckConstraint("visibility IN ('private', 'descendants', 'all')", name="ck_question_pools_visibility"),
        UniqueConstraint("owner_org_id", "name", name="uq_question_pools_owner_name"),
        Index("ix_question_pools_owner_org_id", "owner_org_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    visibility: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'private'"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)


class PoolQuestion(Base):
    """Question rattachée à un pool (toujours une question de l'entité propriétaire du pool)."""

    __tablename__ = "pool_tests"
    __table_args__ = (Index("ix_pool_tests_test_id", "test_id"),)

    pool_id: Mapped[int] = mapped_column(ForeignKey("question_pools.id", ondelete="CASCADE"), primary_key=True)
    test_id: Mapped[int] = mapped_column(ForeignKey("tests.test_id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class PoolInclude(Base):
    """Composition : un pool inclut un autre pool (sans cycle)."""

    __tablename__ = "pool_includes"
    __table_args__ = (
        CheckConstraint("parent_pool_id <> child_pool_id", name="ck_pool_includes_not_self"),
        Index("ix_pool_includes_child_pool_id", "child_pool_id"),
    )

    parent_pool_id: Mapped[int] = mapped_column(ForeignKey("question_pools.id", ondelete="CASCADE"), primary_key=True)
    child_pool_id: Mapped[int] = mapped_column(ForeignKey("question_pools.id", ondelete="CASCADE"), primary_key=True)


class PerimeterPool(Base):
    """Abonnement d'un périmètre (site) à un pool : ses questions s'ajoutent aux
    questions propres du périmètre pour les lancements et planifications."""

    __tablename__ = "perimeter_pools"
    __table_args__ = (Index("ix_perimeter_pools_pool_id", "pool_id"),)

    perimeter_id: Mapped[int] = mapped_column(ForeignKey("perimeters.id", ondelete="CASCADE"), primary_key=True)
    pool_id: Mapped[int] = mapped_column(ForeignKey("question_pools.id", ondelete="CASCADE"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)


class QuestionTheme(Base):
    __tablename__ = "test_themes"
    __table_args__ = (Index("ix_test_themes_theme_id", "theme_id"),)

    test_id: Mapped[int] = mapped_column(ForeignKey("tests.test_id"), primary_key=True)
    theme_id: Mapped[int] = mapped_column(ForeignKey("themes.id", ondelete="CASCADE"), primary_key=True)


class PerimeterTheme(Base):
    __tablename__ = "perimeter_themes"
    __table_args__ = (Index("ix_perimeter_themes_theme_id", "theme_id"),)

    perimeter_id: Mapped[int] = mapped_column(ForeignKey("perimeters.id", ondelete="CASCADE"), primary_key=True)
    theme_id: Mapped[int] = mapped_column(ForeignKey("themes.id", ondelete="CASCADE"), primary_key=True)


# =====================================================================
# ADR-089 §2.6 (chantier E5) — contrats LLM et politique de routage.
# =====================================================================
class LlmContract(Base):
    """Contrat (marché, clé) d'une entité auprès d'un fournisseur, hérité par ses
    descendants. Couvre toute la famille, ou les seuls `model_ids` s'ils sont posés.
    La clé est un blob Fernet (geoeval/web/crypto.py), jamais exposée."""
    __tablename__ = "llm_contracts"
    __table_args__ = (
        CheckConstraint("hosting IS NULL OR hosting IN ('eu', 'non_eu')", name="ck_llm_contracts_hosting"),
        CheckConstraint("valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from", name="ck_llm_contracts_dates"),
        Index("ix_llm_contracts_org_family", "organization_id", "family"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    family: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    reference: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    base_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    api_key_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    extra_headers: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    model_ids: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    valid_from: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    valid_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    cap_eur: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    hosting: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sovereign: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)


class RoutingPolicy(Base):
    """Politique de routage d'une entité, restrictive et héritée : fournisseurs
    autorisés (NULL = pas de restriction), notateurs souverains / hébergés dans l'UE."""
    __tablename__ = "routing_policies"

    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), primary_key=True)
    allowed_families: Mapped[Optional[list[Any]]] = mapped_column(JSONB, nullable=True)
    sovereign_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    eu_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now(),
    )
    updated_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)


# =====================================================================
# ADR-089 §2.8 (chantier E7) — notifications et détecteurs.
# =====================================================================
class Notification(Base):
    """Notification adressée à un utilisateur (in-app, et email selon ses préférences).
    `dedup_key` rend l'émission idempotente par destinataire : un détecteur peut
    repasser sans dupliquer. Un détecteur n'écrit que des notifications, jamais un résultat."""
    __tablename__ = "notifications"
    __table_args__ = (
        Index("uq_notifications_user_dedup", "user_id", "dedup_key", unique=True,
              postgresql_where=text("dedup_key IS NOT NULL")),
        Index("ix_notifications_user_created", "user_id", text("created_at DESC")),
        Index("ix_notifications_org", "organization_id"),
        Index("ix_notifications_digest_pending", "user_id", "created_at",
              postgresql_where=text("email_status = 'digest_pending'")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    organization_id: Mapped[Optional[int]] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    link: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    dedup_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # none (email non demandé) | sent | not_configured | failed | digest_pending (récapitulatif à venir)
    email_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="none")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    read_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class NotificationPreference(Base):
    """Choix d'un utilisateur pour un type (l'in-app est toujours actif) : email
    immédiat, récapitulatif quotidien ou aucun email. Sans ligne, le défaut du type."""
    __tablename__ = "notification_preferences"
    __table_args__ = (
        CheckConstraint("mode IN ('immediate', 'digest', 'none')", name="ck_notification_preferences_mode"),
    )

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    mode: Mapped[str] = mapped_column(Text, nullable=False)


class DetectorSetting(Base):
    """Réglages des détecteurs posés par une entité, hérités par son sous-arbre
    (résolveur du plus proche, champ par champ). NULL = hérité / défaut plateforme."""
    __tablename__ = "detector_settings"
    __table_args__ = (
        CheckConstraint("always_wrong_runs IS NULL OR always_wrong_runs BETWEEN 2 AND 20",
                        name="ck_detector_settings_runs"),
        CheckConstraint("always_wrong_threshold IS NULL OR (always_wrong_threshold >= 0 AND always_wrong_threshold <= 10)",
                        name="ck_detector_settings_threshold"),
        CheckConstraint("citation_drop_points IS NULL OR (citation_drop_points > 0 AND citation_drop_points <= 100)",
                        name="ck_detector_settings_citation_drop"),
    )

    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True)
    always_wrong_runs: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    always_wrong_threshold: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 2), nullable=True)
    # Chute des citations officielles (E7 suite) : écart en points de pourcentage.
    citation_drop_points: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now(),
    )
    updated_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)


# =====================================================================
# ADR-089 §2.9 (chantier E8) — campagnes.
# =====================================================================
class Campaign(Base):
    """Protocole commun défini par une entité et exécuté par les participants qu'elle
    désigne (elle-même ou ses descendants). `protocol` est figé à l'activation :
    questions, IA évaluées, notateurs et répétitions, grilles de notation."""
    __tablename__ = "campaigns"
    __table_args__ = (
        CheckConstraint("status IN ('draft', 'active', 'closed')", name="ck_campaigns_status"),
        Index("ix_campaigns_owner", "owner_org_id"),
        Index("ix_campaigns_due", "status", "next_run_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    source_pool_id: Mapped[Optional[int]] = mapped_column(ForeignKey("question_pools.id", ondelete="SET NULL"), nullable=True)
    # Brouillon : sélection en cours ; actif / clos : instantané figé à l'activation.
    tested_models: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    judges: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    protocol: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    schedule_kind: Mapped[str] = mapped_column(Text, nullable=False)
    schedule_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    next_run_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    activated_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class CampaignParticipant(Base):
    """Entité désignée pour exécuter une campagne (à ses frais, sous ses contrats)."""
    __tablename__ = "campaign_participants"
    __table_args__ = (Index("ix_campaign_participants_org", "organization_id"),)

    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True)
    added_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    last_job_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_skipped_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_skip_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


# =====================================================================
# ADR-089 §2.8 (suite E7) — signalements humains.
# =====================================================================
class TestReport(Base):
    """Signalement d'une question par un membre d'une entité qui la voit (propre, pool,
    campagne, run). Traité par l'entité propriétaire : corrigé ou rejeté, avec réponse."""
    __tablename__ = "test_reports"
    __test__ = False  # pas une classe de test pytest
    __table_args__ = (
        CheckConstraint("category IN ('expected_answer', 'ambiguous', 'obsolete', 'citation', 'other')",
                        name="ck_test_reports_category"),
        CheckConstraint("status IN ('open', 'fixed', 'rejected')", name="ck_test_reports_status"),
        Index("ix_test_reports_owner_status", "owner_org_id", "status"),
        Index("ix_test_reports_test", "test_id"),
        Index("ix_test_reports_reporter_org", "reporter_org_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    test_id: Mapped[int] = mapped_column(ForeignKey("tests.test_id"), nullable=False)
    owner_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    reporter_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    reporter_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("runs.run_id"), nullable=True)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="open")
    resolution_comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
