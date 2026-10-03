"""
Campagnes (ADR-089 §2.9, chantier E8).

Une entité définit un **protocole commun** et désigne les participants qui
l'exécutent : elle-même et / ou ses descendants. Arbitrages E8 :
- **Participants désignés, planification automatique** : le planificateur exécute la
  campagne pour chaque participant, à ses frais et sous ses contrats ; un participant
  bloqué (budget, contrat, routage) est sauté et tracé, comme une programmation.
- **Instantané à l'activation** : questions (depuis un pool visible du propriétaire,
  publiées seulement), versions des IA évaluées, notateurs et répétitions, grilles de
  notation. Modifier le pool ensuite ne change pas la campagne ; tant qu'une campagne
  est active, la grille d'une de ses questions ne peut plus changer.
- **Comparaison participants × IA** : les runs portent `campaign_id`.

Cycle : brouillon (modifiable) → active (protocole figé, participants modifiables)
→ close (plus aucune exécution ; les runs restent).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from geoeval.db.models import (
    AuditLog,
    Campaign,
    CampaignParticipant,
    Model,
    Organization,
    RunEvaluation,
    RunRow,
    Test,
)
from geoeval.web import hierarchy, pools

logger = logging.getLogger("geoeval.web.campaigns")

STATUS_LABELS = {"draft": "brouillon", "active": "active", "closed": "close"}


class CampaignError(ValueError):
    """Opération refusée sur une campagne. `status` : 400 invalide, 404 introuvable, 409 conflit."""

    def __init__(self, detail: str, status: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status


# ---------------------------------------------------------------------
# Lecture
# ---------------------------------------------------------------------
def get(session: Session, campaign_id: int) -> Optional[Campaign]:
    return session.get(Campaign, campaign_id)


def participant_ids(session: Session, campaign_id: int) -> list[int]:
    return list(session.execute(
        select(CampaignParticipant.organization_id).where(CampaignParticipant.campaign_id == campaign_id)
        .order_by(CampaignParticipant.organization_id)
    ).scalars())


def participants(session: Session, campaign_id: int) -> list[tuple[CampaignParticipant, Organization]]:
    rows = session.execute(
        select(CampaignParticipant, Organization)
        .join(Organization, Organization.id == CampaignParticipant.organization_id)
        .where(CampaignParticipant.campaign_id == campaign_id).order_by(Organization.path)
    ).all()
    return [(p, o) for p, o in rows]


def get_for_org(session: Session, org: Organization, campaign_id: int) -> tuple[Campaign, bool]:
    """Campagne visible depuis `org` (propriétaire ou participante), sinon 404.
    Renvoie (campagne, org est-elle propriétaire ?)."""
    c = get(session, campaign_id)
    if c is None:
        raise CampaignError("Campagne introuvable.", 404)
    if c.owner_org_id == org.id:
        return c, True
    if org.id in participant_ids(session, c.id):
        return c, False
    raise CampaignError("Campagne introuvable.", 404)


def get_owned(session: Session, owner: Organization, campaign_id: int) -> Campaign:
    c, owned = get_for_org(session, owner, campaign_id)
    if not owned:
        raise CampaignError("Seule l'entité propriétaire peut modifier cette campagne.", 403)
    return c


def list_for_org(session: Session, org: Organization) -> tuple[list[Campaign], list[tuple[Campaign, Organization]]]:
    """(campagnes possédées, campagnes où l'entité participe sans les posséder + propriétaire)."""
    owned = list(session.execute(
        select(Campaign).where(Campaign.owner_org_id == org.id).order_by(Campaign.created_at.desc())
    ).scalars())
    joined = session.execute(
        select(Campaign, Organization)
        .join(CampaignParticipant, CampaignParticipant.campaign_id == Campaign.id)
        .join(Organization, Organization.id == Campaign.owner_org_id)
        .where(CampaignParticipant.organization_id == org.id, Campaign.owner_org_id != org.id,
               Campaign.status != "draft")
        .order_by(Campaign.created_at.desc())
    ).all()
    return owned, [(c, o) for c, o in joined]


# ---------------------------------------------------------------------
# Écriture (brouillon)
# ---------------------------------------------------------------------
def _models_by_version(session: Session) -> dict[str, Model]:
    return {m.model_version: m for m in session.execute(select(Model)).scalars()}


def _check_selection(session: Session, tested_models: list[str], judges: list[dict[str, Any]]) -> tuple[list[str], list[dict]]:
    catalog = _models_by_version(session)
    tested = [v for v in dict.fromkeys(tested_models or []) if v]
    unknown = [v for v in tested if v not in catalog or not catalog[v].is_active]
    if unknown:
        raise CampaignError(f"IA évaluées inconnues ou désactivées : {unknown}")
    norm: list[dict] = []
    for j in judges or []:
        v, repeats = j.get("model"), max(1, min(10, int(j.get("repeats") or 1)))
        if v not in catalog or not catalog[v].is_active or not catalog[v].is_judge:
            raise CampaignError(f"Notateur inconnu, désactivé ou non notateur : {v!r}")
        if v not in {x["model"] for x in norm}:
            norm.append({"model": v, "repeats": repeats})
    return tested, norm


def _check_participants(session: Session, owner: Organization, org_ids: Iterable[int]) -> list[int]:
    ids = sorted({int(i) for i in org_ids})
    for i in ids:
        org = session.get(Organization, i)
        if org is None or not hierarchy.is_ancestor_or_self(owner, org):
            raise CampaignError(f"Participant {i} hors du sous-arbre de « {owner.name} ».")
    return ids


def _set_participants(session: Session, c: Campaign, ids: list[int]) -> None:
    current = set(participant_ids(session, c.id))
    for i in current - set(ids):
        session.delete(session.get(CampaignParticipant, (c.id, i)))
    for i in set(ids) - current:
        session.add(CampaignParticipant(campaign_id=c.id, organization_id=i))


def create(
    session: Session, owner: Organization, *, name: str, description: Optional[str] = None,
    source_pool_id: Optional[int] = None, tested_models: Iterable[str] = (), judges: Iterable[dict] = (),
    schedule_kind: str, schedule_config: dict[str, Any], participant_ids: Iterable[int] = (),
    created_by: Optional[int] = None,
) -> Campaign:
    """Crée une campagne en brouillon."""
    name = (name or "").strip()
    if not name:
        raise CampaignError("Le nom de la campagne est obligatoire.")
    if source_pool_id is not None:
        try:
            pools.get_visible(session, owner, int(source_pool_id))
        except pools.PoolError as e:
            raise CampaignError(e.detail, e.status)
    tested, norm_judges = _check_selection(session, list(tested_models), list(judges))
    ids = _check_participants(session, owner, participant_ids)
    c = Campaign(owner_org_id=owner.id, name=name, description=(description or "").strip() or None,
                 source_pool_id=source_pool_id, tested_models=tested, judges=norm_judges,
                 schedule_kind=schedule_kind, schedule_config=schedule_config, created_by=created_by)
    session.add(c)
    session.flush()
    _set_participants(session, c, ids)
    session.commit()
    return c


def update(
    session: Session, c: Campaign, *, name: Optional[str] = None, description: Optional[str] = None,
    set_description: bool = False, source_pool_id: Optional[int] = None, set_source_pool: bool = False,
    tested_models: Optional[list[str]] = None, judges: Optional[list[dict]] = None,
    schedule_kind: Optional[str] = None, schedule_config: Optional[dict[str, Any]] = None,
    participant_ids: Optional[Iterable[int]] = None,
) -> Campaign:
    """Brouillon : tout est modifiable. Active : seuls le nom, la description et les
    participants le sont (le protocole est figé). Close : rien."""
    owner = session.get(Organization, c.owner_org_id)
    if c.status == "closed":
        raise CampaignError("Campagne close : plus aucune modification.", 409)
    touches_protocol = any(v is not None for v in (tested_models, judges, schedule_kind, schedule_config)) or set_source_pool
    if c.status == "active" and touches_protocol:
        raise CampaignError("Protocole figé depuis l'activation : crée une nouvelle campagne pour le changer.", 409)
    if name is not None:
        if not name.strip():
            raise CampaignError("Le nom de la campagne est obligatoire.")
        c.name = name.strip()
    if set_description:
        c.description = (description or "").strip() or None
    if set_source_pool:
        if source_pool_id is not None:
            try:
                pools.get_visible(session, owner, int(source_pool_id))
            except pools.PoolError as e:
                raise CampaignError(e.detail, e.status)
        c.source_pool_id = source_pool_id
    if tested_models is not None or judges is not None:
        tested, norm = _check_selection(session, tested_models if tested_models is not None else list(c.tested_models),
                                        judges if judges is not None else list(c.judges))
        c.tested_models, c.judges = tested, norm
    if schedule_kind is not None:
        c.schedule_kind = schedule_kind
    if schedule_config is not None:
        c.schedule_config = schedule_config
    if participant_ids is not None:
        _set_participants(session, c, _check_participants(session, owner, participant_ids))
    session.commit()
    return c


def delete_draft(session: Session, c: Campaign) -> None:
    if c.status != "draft":
        raise CampaignError("Seul un brouillon se supprime ; une campagne active se clôt.", 409)
    session.delete(c)
    session.commit()


# ---------------------------------------------------------------------
# Activation, clôture
# ---------------------------------------------------------------------
def snapshot_tests(session: Session, c: Campaign) -> list[Test]:
    """Questions publiées du pool source, telles que le propriétaire les voit."""
    if c.source_pool_id is None:
        raise CampaignError("Choisis le pool de questions de la campagne.")
    owner = session.get(Organization, c.owner_org_id)
    ids = pools.resolve_pool_tests(session, c.source_pool_id, owner).test_ids
    if not ids:
        return []
    return list(session.execute(
        select(Test).where(Test.test_id.in_(ids), Test.status == "published",
                           Test.expected_answer.is_not(None)).order_by(Test.test_id)
    ).scalars())


def activate(session: Session, c: Campaign, *, now: Optional[datetime] = None) -> Campaign:
    """Brouillon → active : fige le protocole et arme la première échéance."""
    from geoeval.worker import scheduler

    if c.status != "draft":
        raise CampaignError("Seul un brouillon s'active.", 409)
    if not c.tested_models or not c.judges:
        raise CampaignError("Choisis au moins une IA évaluée et un notateur.")
    if not participant_ids(session, c.id):
        raise CampaignError("Désigne au moins un participant.")
    tests = snapshot_tests(session, c)
    if not tests:
        raise CampaignError("Le pool ne contient aucune question publiée et notable.")
    now = now or datetime.now(timezone.utc)
    try:
        next_run = scheduler.compute_next_run(c.schedule_kind, c.schedule_config, after=now)
    except (ValueError, KeyError) as exc:
        raise CampaignError(f"Fréquence invalide : {exc}")
    if next_run is None:
        raise CampaignError("La date d'exécution unique est déjà passée.")
    pool = pools.get(session, c.source_pool_id)
    c.protocol = {
        "test_ids": [t.test_id for t in tests],
        "grids": {str(t.test_id): [t.response_quality_prompt_id, t.citation_quality_prompt_id] for t in tests},
        "tested_models": list(c.tested_models),
        "judges": list(c.judges),
        "source_pool": {"id": c.source_pool_id, "name": pool.name if pool else None},
        "frozen_at": now.isoformat(),
    }
    c.status, c.activated_at, c.next_run_at = "active", now, next_run
    session.commit()
    return c


def close(session: Session, c: Campaign) -> Campaign:
    if c.status != "active":
        raise CampaignError("Seule une campagne active se clôt.", 409)
    c.status, c.closed_at, c.next_run_at = "closed", datetime.now(timezone.utc), None
    session.commit()
    return c


def locked_test_ids(session: Session) -> set[int]:
    """Questions des campagnes actives : leur grille de notation est verrouillée."""
    out: set[int] = set()
    for proto in session.execute(select(Campaign.protocol).where(Campaign.status == "active")).scalars():
        out.update(int(i) for i in (proto or {}).get("test_ids", []))
    return out


def campaign_tests(session: Session, campaign_id: int, org_id: int, test_ids: Optional[list[int]] = None) -> list[Test]:
    """Questions qu'exécute un participant : celles du protocole, encore publiées.
    Vide si l'entité n'est pas participante (garde-fou)."""
    c = get(session, campaign_id)
    if c is None or not c.protocol or org_id not in participant_ids(session, campaign_id):
        return []
    ids = [int(i) for i in c.protocol.get("test_ids", [])]
    if test_ids:
        ids = [i for i in ids if i in {int(t) for t in test_ids}]
    if not ids:
        return []
    return list(session.execute(
        select(Test).where(Test.test_id.in_(ids), Test.status == "published",
                           Test.validity_end_at.is_(None), Test.expected_answer.is_not(None))
        .order_by(Test.test_id)
    ).scalars())


# ---------------------------------------------------------------------
# Exécution
# ---------------------------------------------------------------------
@dataclass
class Execution:
    queued: list[tuple[int, str]] = field(default_factory=list)          # (org_id, job_id)
    skipped: list[tuple[int, str, str]] = field(default_factory=list)    # (org_id, kind, motif)


def execute(session: Session, c: Campaign, *, now: Optional[datetime] = None, commit: bool = True) -> Execution:
    """Met en file une exécution de la campagne pour chaque participant. Un participant
    refusé (budget, contrat, routage) est sauté et tracé. N'avance pas l'échéance."""
    from geoeval.web import launching
    from geoeval.worker import jobs

    if c.status != "active" or not c.protocol:
        raise CampaignError("Seule une campagne active s'exécute.", 409)
    now = now or datetime.now(timezone.utc)
    proto = c.protocol
    out = Execution()
    for part, org in participants(session, c.id):
        params = dict(tested_models=list(proto["tested_models"]), judges=list(proto["judges"]),
                      test_ids=list(proto["test_ids"]))
        try:
            launching.estimate_and_check_budget(session, org.id, params, campaign_id=c.id)
        except launching.LaunchError as exc:
            if exc.kind not in launching.SKIPPABLE_KINDS and exc.kind != "validation":
                raise
            part.last_skipped_at, part.last_skip_reason = now, exc.detail
            session.add(AuditLog(user_id=None, org_id=org.id, action=f"skip_{exc.kind}", entity_type="campaign",
                                 entity_id=c.id, meta_json={"campaign": c.name, "reason": exc.detail}))
            out.skipped.append((org.id, exc.kind, exc.detail))
            logger.warning("campagne %s : participant %s sauté (%s)", c.id, org.slug, exc.detail)
            continue
        job = jobs.submit(session, dict(**params, organization_id=org.id, perimeter_id=None, campaign_id=c.id,
                                        note=f"campagne : {c.name}"), commit=False)
        part.last_job_id, part.last_run_at, part.last_skip_reason = job.id, now, None
        out.queued.append((org.id, job.id))
    c.last_run_at = now
    if commit:
        session.commit()
    return out


def due(session: Session, now: datetime) -> list[Campaign]:
    return list(session.execute(
        select(Campaign).where(Campaign.status == "active", Campaign.next_run_at.is_not(None),
                               Campaign.next_run_at <= now)
    ).scalars())


def tick(session: Session, now: Optional[datetime] = None) -> tuple[int, set[int]]:
    """Planificateur : exécute les campagnes échues et recalcule leur échéance.
    Ne commite pas (l'appelant tient le verrou). Renvoie (jobs mis en file,
    entités sautées pour budget — pour les alertes)."""
    from geoeval.worker import scheduler

    now = now or datetime.now(timezone.utc)
    queued, budget_orgs = 0, set()
    for c in due(session, now):
        try:
            ex = execute(session, c, now=now, commit=False)
        except Exception:  # noqa: BLE001 — une campagne en erreur ne bloque pas les autres
            logger.exception("exécution de la campagne %s en échec", c.id)
            ex = Execution()
        queued += len(ex.queued)
        budget_orgs.update(o for o, kind, _ in ex.skipped if kind == "budget")
        c.next_run_at = scheduler.compute_next_run(c.schedule_kind, c.schedule_config, after=now)
    return queued, budget_orgs


# ---------------------------------------------------------------------
# Résultats
# ---------------------------------------------------------------------
@dataclass
class ResultRow:
    org: Organization
    model_version: str
    n_runs: int
    last_run_id: int
    last_at: Optional[datetime]
    last_response: Optional[float]
    last_citation: Optional[float]
    prev_response: Optional[float] = None
    prev_citation: Optional[float] = None

    @property
    def delta_response(self) -> Optional[float]:
        if self.last_response is None or self.prev_response is None:
            return None
        return round(self.last_response - self.prev_response, 2)

    @property
    def delta_citation(self) -> Optional[float]:
        if self.last_citation is None or self.prev_citation is None:
            return None
        return round(self.last_citation - self.prev_citation, 2)


def _f(x: Any) -> Optional[float]:
    return round(float(x), 2) if x is not None else None


def results(session: Session, c: Campaign, *, only_org_id: Optional[int] = None) -> list[ResultRow]:
    """Par participant × IA évaluée : dernière exécution, précédente, écart et nombre de runs.
    `only_org_id` restreint à un participant (vue d'un participant non propriétaire)."""
    stmt = (
        select(RunRow.run_id, RunRow.organization_id, RunRow.started_at, Model.model_version,
               func.avg(RunEvaluation.response_quality_score), func.avg(RunEvaluation.citation_quality_score))
        .join(Model, Model.model_id == RunRow.tested_model_id)
        .outerjoin(RunEvaluation, RunEvaluation.run_id == RunRow.run_id)
        .where(RunRow.campaign_id == c.id)
        .group_by(RunRow.run_id, RunRow.organization_id, RunRow.started_at, Model.model_version)
        .order_by(RunRow.run_id)
    )
    if only_org_id is not None:
        stmt = stmt.where(RunRow.organization_id == only_org_id)
    series: dict[tuple[int, str], list[tuple]] = {}
    for run_id, org_id, started, version, avg_r, avg_c in session.execute(stmt).all():
        series.setdefault((org_id, version), []).append((run_id, started, _f(avg_r), _f(avg_c)))
    orgs = {o.id: o for o in session.execute(
        select(Organization).where(Organization.id.in_([k[0] for k in series] or [-1]))).scalars()}
    out: list[ResultRow] = []
    for (org_id, version), runs in series.items():
        last = runs[-1]
        row = ResultRow(org=orgs[org_id], model_version=version, n_runs=len(runs), last_run_id=last[0],
                        last_at=last[1], last_response=last[2], last_citation=last[3])
        if len(runs) > 1:
            row.prev_response, row.prev_citation = runs[-2][2], runs[-2][3]
        out.append(row)
    out.sort(key=lambda r: (r.org.path, r.model_version))
    return out
