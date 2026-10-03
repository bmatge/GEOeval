"""Schémas Pydantic de l'API v1 (entrées et sorties). Jamais de secret exposé."""
from __future__ import annotations

from datetime import datetime
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
    evals: list[RunEvaluationOut] = Field(default_factory=list)


class RunDetailOut(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    run_id: int
    started_at: Optional[datetime] = None
    run_meta: Optional[dict[str, Any]] = None
    model_name: str
    model_version: str
    results: list[RunResultOut]


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


class PerimeterPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    kind: Optional[str] = Field(None, max_length=50)
    home_url: Optional[str] = Field(None, max_length=500)
    description: Optional[str] = Field(None, max_length=2000)


class QuestionIn(BaseModel):
    perimeter_id: int
    prompt: str = Field(min_length=1)
    expected_answer: Optional[str] = None
    response_quality_prompt_id: Optional[int] = None
    citation_quality_prompt_id: Optional[int] = None


class QuestionPatch(BaseModel):
    perimeter_id: Optional[int] = None
    prompt: Optional[str] = Field(None, min_length=1)
    expected_answer: Optional[str] = None
    response_quality_prompt_id: Optional[int] = None
    citation_quality_prompt_id: Optional[int] = None


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
