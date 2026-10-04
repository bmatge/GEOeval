"""Schémas Pydantic de l'API v1 (entrées et sorties). Jamais de secret exposé."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Generic, Literal, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


def paginate(items: list[Any], *, limit: int, offset: int) -> dict[str, Any]:
    return dict(items=items[offset: offset + limit], total=len(items), limit=limit, offset=offset)


class _Orm(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---- Identité -------------------------------------------------------
class OrgOut(_Orm):
    id: int
    name: str
    slug: str
    created_at: datetime
    parent_id: Optional[int] = None
    kind: str = "autre"
    depth: int = 0
    path: str = ""
    siret: Optional[str] = None


class OrgRefOut(_Orm):
    id: int
    name: str
    slug: str
    kind: str


class OrgDetailOut(OrgOut):
    lineage: list[OrgRefOut] = Field(default_factory=list, description="Ancêtres, de la racine au parent")


class OrgCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9\-]*$")
    kind: Literal["ministere", "direction", "service", "autre"] = "autre"
    parent_slug: Optional[str] = None
    siret: Optional[str] = Field(None, description="14 chiffres")


class OrgPatch(BaseModel):
    """Champs optionnels. `parent_slug: null` explicite = devenir une racine."""
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    kind: Optional[Literal["ministere", "direction", "service", "autre"]] = None
    parent_slug: Optional[str] = None
    siret: Optional[str] = None


class OrgRoleOut(BaseModel):
    slug: str
    role: str


class TokenRefOut(BaseModel):
    id: int
    name: str
    prefix: str
    role: str


class MeOut(BaseModel):
    kind: Literal["token", "session"]
    user_id: Optional[int] = None
    email: Optional[str] = None
    is_platform_admin: bool = False
    organizations: list[OrgRoleOut] = Field(default_factory=list)
    token: Optional[TokenRefOut] = None


# ---- Corpus ---------------------------------------------------------
class PerimeterOut(_Orm):
    id: int
    name: str
    slug: str
    kind: Optional[str] = None
    home_url: Optional[str] = None
    description: Optional[str] = None
    created_at: datetime
    n_questions: int = 0
    n_pooled_questions: int = 0          # E4 : questions apportées par les pools abonnés
    domains: list[str] = Field(default_factory=list)
    theme_ids: list[int] = Field(default_factory=list)

    @field_validator("domains", mode="before")
    @classmethod
    def _domains_none_as_empty(cls, v):
        return v or []


class GroundTruthOut(_Orm):
    version: int
    reference_answer: str
    reference_urls: list[Any] = Field(default_factory=list)
    valid_from: datetime
    valid_to: Optional[datetime] = None
    notes: Optional[str] = None

    @field_validator("reference_urls", mode="before")
    @classmethod
    def _none_as_empty(cls, v):
        return v or []


class QuestionOut(_Orm):
    test_id: int
    perimeter_id: int
    prompt: str
    expected_answer: Optional[str] = None
    response_quality_prompt_id: Optional[int] = None
    citation_quality_prompt_id: Optional[int] = None
    validity_start_at: datetime
    validity_end_at: Optional[datetime] = None
    is_active: bool = True
    status: str = "published"           # draft | in_review | published | retired (E8)
    theme_ids: list[int] = Field(default_factory=list)
    submitted_at: Optional[datetime] = None
    reviewed_at: Optional[datetime] = None
    review_comment: Optional[str] = None


class EffectiveQuestionOut(QuestionOut):
    """Question effective d'un périmètre (E4) : propre ou issue d'un pool abonné."""
    organization_id: int
    own: bool = True
    pools: list[str] = Field(default_factory=list)


class QuestionDetailOut(QuestionOut):
    ground_truth: Optional[GroundTruthOut] = None


class ModelOut(_Orm):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())
    model_id: int
    model_name: str
    model_version: str
    is_active: bool
    is_judge: bool
    is_sovereign: bool = False
    hosting: Optional[str] = None
    search_config: Optional[dict[str, Any]] = None
    testable: bool = False


# ---- Évaluations ----------------------------------------------------
class RunOut(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    run_id: int
    started_at: Optional[datetime] = None
    run_meta: Optional[dict[str, Any]] = None
    model_name: str
    model_version: str
    n_evals: int
    avg_response: Optional[float] = None
    avg_citation: Optional[float] = None


class RunEvaluationOut(BaseModel):
    judge_version: str
    judge_run_index: int
    response_score: Optional[float] = None
    response_label: Optional[str] = None
    citation_score: Optional[float] = None
    citation_label: Optional[str] = None


class RunResultOut(BaseModel):
    test_id: int
    prompt: str
    expected_answer: Optional[str] = None
    raw_answer: str
    raw_citations: list[Any] = Field(default_factory=list)
    official_share: Optional[float] = Field(None, description="Part des citations sur les domaines officiels du périmètre (E4)")
    evals: list[RunEvaluationOut] = Field(default_factory=list)


class RunDetailOut(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    run_id: int
    started_at: Optional[datetime] = None
    run_meta: Optional[dict[str, Any]] = None
    model_name: str
    model_version: str
    results: list[RunResultOut]
    official_domains: list[str] = Field(default_factory=list)
    official_share: Optional[float] = Field(None, description="None si le périmètre ne déclare aucun domaine ou sans citation")


class LaunchIn(BaseModel):
    perimeter_id: int
    tested_models: list[str] = Field(min_length=1, description="model_version des IA évaluées")
    judge_models: list[str] = Field(min_length=1, description="model_version des notateurs")
    repeats: int = Field(1, ge=1, le=10)
    test_ids: list[int] = Field(min_length=1)
    note: Optional[str] = Field(None, max_length=500)


# ---- Statistiques ---------------------------------------------------
class StatsSummaryOut(BaseModel):
    n_runs: int
    n_evals: int
    avg_response: Optional[float] = None
    avg_citation: Optional[float] = None
    n_models: int


class LeaderboardRowOut(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    model_id: int
    model_name: str
    model_version: str
    n_runs: int
    n_evals: int
    avg_response: Optional[float] = None
    avg_citation: Optional[float] = None


class QuestionStatOut(BaseModel):
    test_id: int
    label: str
    prompt: str
    n_evals: int
    avg_response: Optional[float] = None
    avg_citation: Optional[float] = None


class EvolutionPointOut(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    label: str
    seq: int
    model: str
    run_id: int
    avg_response: Optional[float] = None
    avg_citation: Optional[float] = None


# ---- Planifications et jobs ----------------------------------------
class ScheduleOut(_Orm):
    schedule_id: int
    perimeter_id: int
    name: str
    tested_models: list[Any]
    judges: list[Any]
    test_ids: Optional[list[Any]] = None
    note: Optional[str] = None
    schedule_kind: str
    schedule_config: dict[str, Any]
    enabled: bool
    next_run_at: Optional[datetime] = None
    last_run_at: Optional[datetime] = None
    last_job_id: Optional[str] = None
    last_skipped_at: Optional[datetime] = None
    last_skip_reason: Optional[str] = None
    created_at: datetime
    description: str = ""


class JobOut(BaseModel):
    id: str
    status: str
    phase: str
    current: int
    total: int
    pct: int
    error: Optional[str] = None
    run_ids: list[int] = Field(default_factory=list)
    created_at: Optional[str] = None
    params: dict[str, Any] = Field(default_factory=dict)
    worker_id: Optional[str] = None
    attempt: int = 0
    log: list[str] = Field(default_factory=list)


# ---- Jetons ---------------------------------------------------------
class TokenCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    role: Literal["viewer", "editor", "org_admin"] = "viewer"
    expires_in_days: Optional[int] = Field(None, ge=1, le=3650)


class TokenOut(_Orm):
    id: int
    name: str
    role: str
    prefix: str
    created_at: datetime
    expires_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    active: bool = True


class TokenCreatedOut(TokenOut):
    token: str = Field(description="Jeton en clair, affiché une seule fois.")


# ---- Écriture du corpus (lot 1.3c) ----------------------------------
class PerimeterIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9\-]*$")
    kind: Optional[str] = Field(None, max_length=50)
    home_url: Optional[str] = Field(None, max_length=500)
    description: Optional[str] = Field(None, max_length=2000)
    domains: list[str] = Field(default_factory=list, description="Domaines officiels (sous-domaines inclus)")
    theme_ids: list[int] = Field(default_factory=list)


class PerimeterPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    kind: Optional[str] = Field(None, max_length=50)
    home_url: Optional[str] = Field(None, max_length=500)
    description: Optional[str] = Field(None, max_length=2000)
    domains: Optional[list[str]] = None
    theme_ids: Optional[list[int]] = None


class QuestionIn(BaseModel):
    perimeter_id: int
    prompt: str = Field(min_length=1)
    expected_answer: Optional[str] = None
    response_quality_prompt_id: Optional[int] = None
    citation_quality_prompt_id: Optional[int] = None
    theme_ids: list[int] = Field(default_factory=list)
    status: Literal["draft", "published"] = Field("published", description="Brouillon : hors runs, pools et campagnes")


class QuestionPatch(BaseModel):
    perimeter_id: Optional[int] = None
    prompt: Optional[str] = Field(None, min_length=1)
    expected_answer: Optional[str] = None
    response_quality_prompt_id: Optional[int] = None
    citation_quality_prompt_id: Optional[int] = None
    theme_ids: Optional[list[int]] = None


# ---- Pools et thèmes (E4) ------------------------------------------
class ThemeOut(_Orm):
    id: int
    slug: str
    label: str


class ThemeIn(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    slug: Optional[str] = Field(None, min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9\-]*$")


class ThemePatch(BaseModel):
    label: str = Field(min_length=1, max_length=200)


PoolVisibility = Literal["private", "descendants", "all"]


class PoolOut(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    visibility: PoolVisibility
    owner_org_id: int
    owner_org_slug: str
    created_at: datetime
    test_ids: list[int] = Field(default_factory=list, description="Questions directement dans le pool")
    included_pool_ids: list[int] = Field(default_factory=list)
    n_questions: int = Field(0, description="Questions effectives pour l'entité appelante (inclusions visibles comprises)")


class PoolIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    visibility: PoolVisibility = "private"


class PoolPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    visibility: Optional[PoolVisibility] = None


class PoolTestsIn(BaseModel):
    test_ids: list[int] = Field(min_length=1)


class PoolRefIn(BaseModel):
    pool_id: int


class SubscriptionOut(BaseModel):
    pool_id: int
    pool_name: str
    owner_org_slug: str
    visible: bool = Field(description="False si le pool n'est plus partagé avec l'entité : il ne fournit plus de questions")
    n_questions: int = 0


class GroundTruthIn(BaseModel):
    reference_answer: str = Field(min_length=1)
    reference_urls: list[str] = Field(default_factory=list)
    notes: Optional[str] = None


class ScheduleIn(BaseModel):
    """Création d'une planification. `schedule_kind` ∈ once | daily | weekly | every_n_hours ;
    `at` (YYYY-MM-DDTHH:MM, heure de Paris) pour once, `time` (HH:MM) pour daily et weekly,
    `weekday` (0 = lundi) pour weekly, `hours` pour every_n_hours."""
    perimeter_id: int
    name: str = Field(min_length=1, max_length=200)
    tested_models: list[str] = Field(min_length=1)
    judge_models: list[str] = Field(min_length=1)
    repeats: int = Field(1, ge=1, le=10)
    test_ids: list[int] = Field(min_length=1)
    note: Optional[str] = Field(None, max_length=500)
    schedule_kind: Literal["once", "daily", "weekly", "every_n_hours"]
    at: Optional[str] = None
    time: Optional[str] = None
    weekday: Optional[int] = Field(None, ge=0, le=6)
    hours: Optional[int] = Field(None, ge=1)


class SchedulePatch(BaseModel):
    enabled: Optional[bool] = None
    name: Optional[str] = Field(None, min_length=1, max_length=200)


# ---- Budget consolidé (E3) ------------------------------------------
class BudgetConstraintOut(BaseModel):
    owner_slug: str
    owner_name: str
    inherited: bool
    period: Literal["month", "day"]
    cap_eur: float
    spent_eur: float = Field(description="Dépense consolidée du sous-arbre de l'entité qui porte le plafond")
    ratio: Optional[float] = None
    level: Literal["ok", "warning", "exceeded"]


class BudgetAlertOut(_Orm):
    organization_id: int
    period: str
    period_key: str
    threshold: int
    spent_eur: float
    cap_eur: float
    created_at: datetime
    email_status: str


class BudgetOut(BaseModel):
    monthly_cap_eur: Optional[float] = None
    daily_cap_eur: Optional[float] = None
    month_spent_eur: float = Field(description="Dépense consolidée du mois (entité + sous-entités)")
    day_spent_eur: float
    own_month_spent_eur: float = Field(description="Dépense du mois de l'entité seule")
    constraints: list[BudgetConstraintOut]
    alerts: list[BudgetAlertOut]


# ---- Contrats LLM et politique de routage (E5) ----------------------
LlmFamily = Literal["openrouter", "openai", "mistral", "gemini", "albert", "generic"]
Hosting = Literal["eu", "non_eu"]


class ContractOut(BaseModel):
    """Contrat LLM. La clé n'est jamais renvoyée (`has_api_key` seulement)."""
    id: int
    owner_org_slug: str
    inherited: bool = False
    family: str
    label: str
    reference: Optional[str] = None
    base_url: Optional[str] = None
    has_api_key: bool = False
    header_names: list[str] = Field(default_factory=list)
    model_ids: list[int] = Field(default_factory=list)
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    cap_eur: Optional[Decimal] = None
    spent_eur: Decimal = Decimal("0")
    hosting: Optional[str] = None
    sovereign: bool = False
    is_active: bool = True
    status: Literal["active", "upcoming", "expired", "exhausted", "inactive"]


class ContractIn(BaseModel):
    family: LlmFamily
    label: str = Field(min_length=1, max_length=200)
    reference: Optional[str] = Field(None, max_length=200)
    base_url: Optional[str] = Field(None, max_length=500)
    api_key: Optional[str] = Field(None, description="Écriture seule, chiffrée au repos")
    extra_headers: Optional[dict[str, str]] = None
    model_ids: list[int] = Field(default_factory=list, description="Vide = toute la famille")
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    cap_eur: Optional[Decimal] = Field(None, ge=0, description="Plafond sur toute la durée du contrat")
    hosting: Optional[Hosting] = None
    sovereign: bool = False
    is_active: bool = True


class ContractPatch(BaseModel):
    label: Optional[str] = Field(None, min_length=1, max_length=200)
    reference: Optional[str] = Field(None, max_length=200)
    base_url: Optional[str] = Field(None, max_length=500)
    api_key: Optional[str] = Field(None, description="Nouvelle clé (écriture seule)")
    clear_api_key: bool = False
    extra_headers: Optional[dict[str, str]] = None
    model_ids: Optional[list[int]] = None
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    cap_eur: Optional[Decimal] = Field(None, ge=0)
    hosting: Optional[Hosting] = None
    sovereign: Optional[bool] = None
    is_active: Optional[bool] = None


class KeyResolutionOut(BaseModel):
    """Clé qu'utiliserait un appel de ce modèle pour l'entité."""
    model_config = ConfigDict(protected_namespaces=())
    model_id: int
    model_version: str
    source: Literal["contract", "platform", "blocked"]
    contract_id: Optional[int] = None
    owner_org_slug: Optional[str] = None
    blocked_reason: Optional[str] = None


class RoutingPolicyIn(BaseModel):
    allowed_families: Optional[list[LlmFamily]] = Field(None, description="None = pas de restriction à ce niveau")
    sovereign_only: bool = False
    eu_only: bool = False


class RoutingPolicyOut(BaseModel):
    own: RoutingPolicyIn
    effective_allowed_families: Optional[list[str]] = None
    effective_sovereign_only: bool = False
    effective_eu_only: bool = False


# ---- Notifications et détecteurs (E7) ------------------------------
class NotificationOut(_Orm):
    id: int
    organization_id: Optional[int] = None
    kind: str
    title: str
    body: str = ""
    link: Optional[str] = None
    payload: Optional[dict[str, Any]] = None
    email_status: str
    created_at: datetime
    read_at: Optional[datetime] = None


class NotificationListOut(BaseModel):
    unread: int
    items: list[NotificationOut]


class NotificationPreferencesIO(BaseModel):
    """Mode d'email par type (l'in-app est toujours actif) : immediate | digest | none."""
    modes: dict[str, Literal["immediate", "digest", "none"]]


class DetectorSettingsIn(BaseModel):
    always_wrong_runs: Optional[int] = Field(None, ge=2, le=20, description="None = hériter")
    always_wrong_threshold: Optional[Decimal] = Field(None, ge=0, le=10, description="None = hériter")
    citation_drop_points: Optional[Decimal] = Field(
        None, gt=0, le=100, description="Écart (points de %) sous la moyenne des 3 runs précédents ; None = hériter")
    calibration_min_rho: Optional[Decimal] = Field(
        None, ge=0, le=1, description="Corrélation de rang minimale notateur / gold ; None = hériter")


class DetectorSettingsOut(BaseModel):
    own: DetectorSettingsIn
    effective_runs: int
    effective_threshold: Decimal
    effective_citation_drop_points: Decimal
    effective_calibration_min_rho: Decimal
    runs_from_org_slug: Optional[str] = None
    threshold_from_org_slug: Optional[str] = None
    citation_drop_from_org_slug: Optional[str] = None
    calibration_from_org_slug: Optional[str] = None


# ---- Signalements (suite E7) ----------------------------------------
class TestReportIn(BaseModel):
    __test__ = False
    test_id: int
    run_id: Optional[int] = None
    category: Literal["expected_answer", "ambiguous", "obsolete", "citation", "other"]
    comment: str = Field("", max_length=2000)


class TestReportResolveIn(BaseModel):
    __test__ = False
    status: Literal["fixed", "rejected"]
    comment: str = Field(min_length=1, max_length=2000)


class TestReportOut(BaseModel):
    __test__ = False
    id: int
    test_id: int
    test_prompt: Optional[str] = None
    owner_org_slug: str
    reporter_org_slug: str
    reporter_email: Optional[str] = None
    run_id: Optional[int] = None
    category: str
    comment: str = ""
    status: Literal["open", "fixed", "rejected"]
    resolution_comment: Optional[str] = None
    resolved_at: Optional[datetime] = None
    created_at: datetime
    owned: bool


# ---- Campagnes (E8) ------------------------------------------------
class CampaignIn(BaseModel):
    """Création d'une campagne en brouillon (mêmes champs de fréquence qu'une planification)."""
    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    source_pool_id: Optional[int] = None
    tested_models: list[str] = Field(default_factory=list)
    judge_models: list[str] = Field(default_factory=list)
    repeats: int = Field(1, ge=1, le=10)
    participant_ids: list[int] = Field(default_factory=list)
    schedule_kind: Literal["once", "daily", "weekly", "every_n_hours"]
    at: Optional[str] = None
    time: Optional[str] = None
    weekday: Optional[int] = Field(None, ge=0, le=6)
    hours: Optional[int] = Field(None, ge=1)


class CampaignPatch(BaseModel):
    """Brouillon : tout ; active : nom, description, participants (protocole figé)."""
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    source_pool_id: Optional[int] = None
    tested_models: Optional[list[str]] = None
    judge_models: Optional[list[str]] = None
    repeats: Optional[int] = Field(None, ge=1, le=10)
    participant_ids: Optional[list[int]] = None


class CampaignParticipantOut(BaseModel):
    org_slug: str
    org_name: str
    last_job_id: Optional[str] = None
    last_run_at: Optional[datetime] = None
    last_skip_reason: Optional[str] = None


class CampaignOut(BaseModel):
    id: int
    owner_org_slug: str
    owned: bool
    name: str
    description: Optional[str] = None
    status: Literal["draft", "active", "closed"]
    source_pool_id: Optional[int] = None
    tested_models: list[str] = Field(default_factory=list)
    judges: list[dict[str, Any]] = Field(default_factory=list)
    protocol: Optional[dict[str, Any]] = None
    schedule_kind: str
    schedule_config: dict[str, Any]
    next_run_at: Optional[datetime] = None
    last_run_at: Optional[datetime] = None
    activated_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    participants: list[CampaignParticipantOut] = Field(default_factory=list)


class CampaignResultOut(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    org_slug: str
    model_version: str
    n_runs: int
    last_run_id: int
    last_at: Optional[datetime] = None
    last_response: Optional[float] = None
    last_citation: Optional[float] = None
    delta_response: Optional[float] = None
    delta_citation: Optional[float] = None


class CampaignExecutionOut(BaseModel):
    queued: list[dict[str, Any]]
    skipped: list[dict[str, Any]]


# ---- Rejugement et calibration (suite E8) ----------------------------
class EvaluationBatchIn(BaseModel):
    """Lot de rejugement : runs de l'entité, notateurs, grilles imposées optionnelles."""
    run_ids: list[int] = Field(min_length=1, max_length=100)
    judge_models: list[str] = Field(min_length=1)
    repeats: int = Field(1, ge=1, le=5)
    response_prompt_id: Optional[int] = None
    citation_prompt_id: Optional[int] = None
    label: Optional[str] = Field(None, max_length=200)


class ComparisonLineOut(BaseModel):
    n_pairs: int
    orig_response: Optional[float] = None
    new_response: Optional[float] = None
    delta_response: Optional[float] = None
    abs_delta_response: Optional[float] = None
    orig_citation: Optional[float] = None
    new_citation: Optional[float] = None
    delta_citation: Optional[float] = None
    rank_correlation: Optional[float] = None
    tested_model: Optional[str] = None


class ComparisonPairOut(BaseModel):
    run_id: int
    test_id: int
    tested_model: str
    orig_response: Optional[float] = None
    new_response: Optional[float] = None
    delta_response: Optional[float] = None
    orig_citation: Optional[float] = None
    new_citation: Optional[float] = None
    delta_citation: Optional[float] = None


class EvaluationBatchOut(BaseModel):
    id: int
    label: Optional[str] = None
    status: str
    run_ids: list[int]
    judges: list[dict[str, Any]]
    response_prompt_id: Optional[int] = None
    citation_prompt_id: Optional[int] = None
    grids: dict[str, Any]
    estimate_eur: Optional[Decimal] = None
    job_id: Optional[str] = None
    created_at: datetime
    promoted_at: Optional[datetime] = None
    promoted_run_ids: list[int] = Field(default_factory=list)
    summary: Optional[ComparisonLineOut] = None
    by_model: list[ComparisonLineOut] = Field(default_factory=list)
    pairs: list[ComparisonPairOut] = Field(default_factory=list)


class PromotionOut(BaseModel):
    promoted_run_ids: list[int]
    skipped: list[dict[str, Any]] = Field(default_factory=list, description="[{run_id, reason}]")
    batch: EvaluationBatchOut


class AnnotationIn(BaseModel):
    run_id: int
    test_id: int
    response_label: Literal["conforme", "partiel", "non_conforme", "hors_sujet"]
    response_score: Decimal = Field(ge=0, le=10)
    citation_label: Literal["conforme", "partiel", "non_conforme", "hors_sujet"]
    citation_score: Decimal = Field(ge=0, le=10)
    notes: Optional[str] = Field(None, max_length=2000)


class AnnotationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    run_id: int
    test_id: int
    organization_id: Optional[int] = None
    annotator_email: str
    response_label: str
    response_score: Decimal
    citation_label: str
    citation_score: Decimal
    notes: Optional[str] = None
    annotated_at: datetime


class AgreementMetricsOut(BaseModel):
    n_pairs: int
    response_spearman: Optional[float] = None
    response_kappa: Optional[float] = None
    response_mae: Optional[float] = None
    citation_spearman: Optional[float] = None
    citation_kappa: Optional[float] = None
    theme_id: Optional[int] = None
    label: Optional[str] = None


class JudgeAgreementOut(BaseModel):
    model_version: str
    batch_id: Optional[int] = None
    overall: AgreementMetricsOut
    by_theme: list[AgreementMetricsOut] = Field(default_factory=list)
    below_threshold: bool


class CalibrationOut(BaseModel):
    n_gold_pairs: int
    threshold: Decimal
    threshold_from_org_slug: Optional[str] = None
    min_pairs: int
    judges: list[JudgeAgreementOut]


# ---- Validation métier (fin de E8) -----------------------------------
class ReviewRejectIn(BaseModel):
    comment: str = Field(min_length=1, max_length=2000)


class ReviewSettingsIO(BaseModel):
    """`review_required` : réglage propre (None = hériter). Lecture : réglage effectif et source."""
    review_required: Optional[bool] = None
    validator_user_ids: list[int] = Field(default_factory=list, description="Validateurs désignés (membres directs)")
    effective_required: Optional[bool] = None
    source_org_slug: Optional[str] = None


# ---- Explorateur de scores -------------------------------------------
class ScoreRowOut(BaseModel):
    """Notes moyennes des notateurs pour une question d'une évaluation."""
    entite: str
    perimetre: str
    ia: str
    question: str
    question_id: int
    evaluation: str
    run_id: int
    date: Optional[str] = None
    note_reponse: Optional[float] = None
    note_citations: Optional[float] = None
    notes: int
    lien: str
