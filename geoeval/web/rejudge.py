"""
Rejugement versionné de l'historique (ADR-089 §2.9, suite E8).

Arbitrages : **comparaison, origine inchangée** — un lot de rejugement note à nouveau
des runs passés avec d'autres notateurs (ou une grille imposée) ; ses notes vont dans
`rejudge_evaluations`, jamais dans `run_evaluations`, et les tableaux de bord continuent
de lire les notes d'origine. Le lot s'affiche à côté, avec les écarts. **Editor+,
contrôles habituels** — mêmes règles qu'un lancement : liste blanche des notateurs,
routage, contrats, devis (appels des notateurs seulement) et budget consolidé. Le lot
s'exécute dans le worker (job `kind = "rejudge"`).

Versions épinglées : le lot fige à sa création les versions des notateurs et
l'empreinte (SHA-256) de chaque grille utilisée.

Promotion (fin de E8) : **échange réversible**, réservé aux org_admin, **hors runs de
campagne** (leur protocole fige les notateurs). Promouvoir un lot archive d'abord les
notes d'origine de chaque run dans un lot `kind = "origin"` (une seule fois par run),
puis installe les notes du lot dans `run_evaluations` : tableaux de bord, statistiques,
API suivent sans autre changement. Revenir en arrière restaure les notes d'origine.
Rien n'est perdu : les notes quittent `run_evaluations` seulement après avoir été
copiées dans l'archive, dans la même transaction.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from datetime import datetime, timezone

from sqlalchemy import delete, func, insert, literal, select
from sqlalchemy.orm import Session

from geoeval.db.models import (
    EvaluationBatch,
    EvaluationPrompt,
    Job,
    Model,
    Organization,
    RejudgeEvaluation,
    RunEvaluation,
    RunResult,
    RunRow,
    Test,
)
from geoeval.web import agreement, budget, launching, pricing
from geoeval.web.launching import LaunchError

MAX_RUNS = 100
MAX_REPEATS = 5
RESPONSE_PROMPT_TYPE, CITATION_PROMPT_TYPE = 1, 2


# ---------------------------------------------------------------------
# Création (contrôles, devis, mise en file)
# ---------------------------------------------------------------------
def _evaluable_tests(session: Session, run_ids: list[int]) -> list[Test]:
    """Questions notables des runs (une entrée par résultat : le devis compte chaque appel)."""
    return list(session.execute(
        select(Test).join(RunResult, RunResult.test_id == Test.test_id)
        .where(RunResult.run_id.in_(run_ids), Test.expected_answer.is_not(None), Test.expected_answer != "")
    ).scalars())


def _prompt(session: Session, prompt_id: Optional[int], prompt_type: int, label: str) -> Optional[EvaluationPrompt]:
    if not prompt_id:
        return None
    p = session.get(EvaluationPrompt, prompt_id)
    if p is None or p.prompt_type_id != prompt_type:
        raise LaunchError("validation", f"Grille {label} invalide (#{prompt_id}).")
    return p


def _grid_snapshot(session: Session, run_ids: list[int], resp: Optional[EvaluationPrompt],
                   cit: Optional[EvaluationPrompt]) -> dict[str, Any]:
    """Empreinte des grilles qui serviront : imposées, sinon celles des questions."""
    ids: set[int] = {p.prompt_id for p in (resp, cit) if p is not None}
    tests = _evaluable_tests(session, run_ids)
    if resp is None:
        ids |= {t.response_quality_prompt_id for t in tests if t.response_quality_prompt_id}
    if cit is None:
        ids |= {t.citation_quality_prompt_id for t in tests if t.citation_quality_prompt_id}
    out: dict[str, Any] = {}
    for p in session.execute(select(EvaluationPrompt).where(EvaluationPrompt.prompt_id.in_(ids or [-1]))).scalars():
        out[str(p.prompt_id)] = {"name": p.prompt_name,
                                 "sha256": hashlib.sha256(p.prompt_text.encode("utf-8")).hexdigest()}
    return out


def prepare(
    session: Session, org: Organization, *, run_ids: list[int], judge_models: list[str], repeats: int = 1,
    response_prompt_id: Optional[int] = None, citation_prompt_id: Optional[int] = None,
    role: Optional[str], is_platform_admin: bool,
) -> dict[str, Any]:
    """Valide la demande et calcule le devis. Lève LaunchError (validation,
    forbidden_models, routing, contract, budget). Renvoie les paramètres du lot."""
    run_ids = sorted({int(r) for r in run_ids})
    if not run_ids:
        raise LaunchError("validation", "Sélectionne au moins un run à rejuger.")
    if len(run_ids) > MAX_RUNS:
        raise LaunchError("validation", f"Au plus {MAX_RUNS} runs par lot.")
    if not judge_models:
        raise LaunchError("validation", "Sélectionne au moins un notateur.")
    if not 1 <= int(repeats) <= MAX_REPEATS:
        raise LaunchError("validation", f"Répétitions : de 1 à {MAX_REPEATS}.")
    owned = set(session.execute(
        select(RunRow.run_id).where(RunRow.run_id.in_(run_ids), RunRow.organization_id == org.id)
    ).scalars())
    missing = [r for r in run_ids if r not in owned]
    if missing:
        raise LaunchError("not_found", f"Runs introuvables pour cette entité : {missing}.")
    with_results = set(session.execute(
        select(RunResult.run_id).where(RunResult.run_id.in_(run_ids)).distinct()).scalars())
    empty = [r for r in run_ids if r not in with_results]
    if empty:
        raise LaunchError("validation", f"Runs sans réponse enregistrée, impossibles à rejuger : {empty}.")
    allowed = launching.allowed_model_versions(session, org.id, role=role, is_platform_admin=is_platform_admin)
    by_version = {m.model_version: m for m in session.execute(
        select(Model).where(Model.model_version.in_(judge_models), Model.is_active.is_(True))).scalars()}
    unknown = [v for v in judge_models if v not in by_version or not by_version[v].is_judge]
    if unknown:
        raise LaunchError("validation", f"Notateurs inconnus ou inactifs : {sorted(unknown)}.")
    if allowed is not None and set(judge_models) - allowed:
        forbidden = sorted(set(judge_models) - allowed)
        raise LaunchError("forbidden_models", f"Modèles non autorisés pour cette organisation : {forbidden}",
                          forbidden=forbidden)
    resp = _prompt(session, response_prompt_id, RESPONSE_PROMPT_TYPE, "de réponse")
    cit = _prompt(session, citation_prompt_id, CITATION_PROMPT_TYPE, "de citations")
    judges = [{"model": v, "repeats": int(repeats)} for v in dict.fromkeys(judge_models)]
    params = {"tested_models": [], "judges": judges}
    launching.check_compliance(session, org.id, params)
    tests = _evaluable_tests(session, run_ids)
    if not tests:
        raise LaunchError("validation", "Aucune question notable dans ces runs (réponse attendue absente).")
    estimate = pricing.estimate_scan_cost(session, org_id=org.id, tests=tests, tested_models=[], judges=judges)
    launching._check_contract_caps(session, estimate)
    check = budget.check_budget(session, org_id=org.id, estimate_eur=estimate["total_eur"])
    if not check.ok:
        raise LaunchError("budget", check.reason, estimate_eur=str(estimate["total_eur"]))
    return dict(
        run_ids=run_ids,
        judges=[{"model_id": by_version[j["model"]].model_id, "model_version": j["model"], "repeats": j["repeats"]}
                for j in judges],
        response_prompt_id=resp.prompt_id if resp else None,
        citation_prompt_id=cit.prompt_id if cit else None,
        grids=_grid_snapshot(session, run_ids, resp, cit),
        estimate=estimate,
        n_evaluations=len(tests) * sum(j["repeats"] for j in judges),
    )


def create(
    session: Session, org: Organization, *, run_ids: list[int], judge_models: list[str], repeats: int = 1,
    response_prompt_id: Optional[int] = None, citation_prompt_id: Optional[int] = None,
    label: Optional[str] = None, created_by: Optional[int] = None, role: Optional[str], is_platform_admin: bool,
) -> EvaluationBatch:
    """Contrôles + devis, puis enregistrement du lot et mise en file du job."""
    from geoeval.worker import jobs as jobqueue

    p = prepare(session, org, run_ids=run_ids, judge_models=judge_models, repeats=repeats,
                response_prompt_id=response_prompt_id, citation_prompt_id=citation_prompt_id,
                role=role, is_platform_admin=is_platform_admin)
    batch = EvaluationBatch(
        organization_id=org.id, label=(label or "").strip()[:200] or None, run_ids=p["run_ids"], judges=p["judges"],
        response_prompt_id=p["response_prompt_id"], citation_prompt_id=p["citation_prompt_id"], grids=p["grids"],
        estimate_eur=p["estimate"]["total_eur"], created_by=created_by,
    )
    session.add(batch)
    session.flush()
    job = jobqueue.submit(session, {
        "organization_id": org.id, "kind": "rejudge", "batch_id": batch.id, "tested_models": [],
        "judges": [{"model": j["model_version"], "repeats": j["repeats"]} for j in p["judges"]],
    }, commit=False)
    batch.job_id = job.id
    session.commit()
    return batch


# ---------------------------------------------------------------------
# Lecture
# ---------------------------------------------------------------------
def list_for_org(session: Session, org: Organization, *, limit: int = 100) -> list[EvaluationBatch]:
    return list(session.execute(
        select(EvaluationBatch).where(EvaluationBatch.organization_id == org.id, EvaluationBatch.kind == "rejudge")
        .order_by(EvaluationBatch.created_at.desc(), EvaluationBatch.id.desc()).limit(limit)
    ).scalars())


def get_for_org(session: Session, org: Organization, batch_id: int) -> EvaluationBatch:
    b = session.get(EvaluationBatch, batch_id)
    if b is None or b.organization_id != org.id or b.kind != "rejudge":
        raise LaunchError("not_found", "Lot de rejugement introuvable.")
    return b


def status(session: Session, batch: EvaluationBatch) -> str:
    """État du lot = état de son job (queued, running, done, error)."""
    job = session.get(Job, batch.job_id) if batch.job_id else None
    return job.status if job is not None else "unknown"


def runs_with_batches(session: Session, run_ids: list[int]) -> dict[int, list[int]]:
    """run_id → lots qui l'ont rejugé (pour signaler un rejugement sur le détail d'un run)."""
    out: dict[int, list[int]] = {}
    for rid, bid in session.execute(
        select(RejudgeEvaluation.run_id, RejudgeEvaluation.batch_id)
        .join(EvaluationBatch, EvaluationBatch.id == RejudgeEvaluation.batch_id)
        .where(RejudgeEvaluation.run_id.in_(run_ids or [-1]), EvaluationBatch.kind == "rejudge")
        .distinct()
    ).all():
        out.setdefault(rid, []).append(bid)
    return out


# ---------------------------------------------------------------------
# Comparaison origine / lot
# ---------------------------------------------------------------------
def _avg(values: list[Optional[float]]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _delta(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return None if a is None or b is None else b - a


@dataclass
class Comparison:
    """Notes moyennes (tous notateurs confondus) d'origine et du lot, par paire (run, question)."""
    pairs: list[dict[str, Any]] = field(default_factory=list)
    by_model: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)


def compare(session: Session, batch: EvaluationBatch, *, top: int = 50) -> Comparison:
    """Écarts par paire, par IA évaluée et au global. `pairs` : les plus grands écarts d'abord."""
    def _scores(model, extra):
        rows = session.execute(
            select(model.run_id, model.test_id, func.avg(model.response_quality_score),
                   func.avg(model.citation_quality_score))
            .where(model.run_id.in_(batch.run_ids or [-1]), *extra)
            .group_by(model.run_id, model.test_id)
        ).all()
        return {(r, t): (float(a) if a is not None else None, float(c) if c is not None else None)
                for r, t, a, c in rows}

    runs = {r.run_id: r for r in session.execute(select(RunRow).where(RunRow.run_id.in_(batch.run_ids or [-1]))).scalars()}
    # Notes d'origine : `run_evaluations`, ou leur archive quand un lot a été promu sur le run.
    orig = _scores(RunEvaluation, [])
    for origin_id in {r.origin_batch_id for r in runs.values() if r.reference_batch_id and r.origin_batch_id}:
        archived = _scores(RejudgeEvaluation, [RejudgeEvaluation.batch_id == origin_id])
        promoted = {r.run_id for r in runs.values() if r.reference_batch_id and r.origin_batch_id == origin_id}
        orig = {k: v for k, v in orig.items() if k[0] not in promoted}
        orig.update({k: v for k, v in archived.items() if k[0] in promoted})
    new = _scores(RejudgeEvaluation, [RejudgeEvaluation.batch_id == batch.id])
    models = {m.model_id: m for m in session.execute(
        select(Model).where(Model.model_id.in_({r.tested_model_id for r in runs.values()} or {-1}))).scalars()}
    tests = {t.test_id: t for t in session.execute(
        select(Test).where(Test.test_id.in_({t for _, t in new} or {-1}))).scalars()}
    pairs = []
    for key, (n_resp, n_cit) in new.items():
        o_resp, o_cit = orig.get(key, (None, None))
        run = runs.get(key[0])
        model = models.get(run.tested_model_id) if run else None
        test = tests.get(key[1])
        pairs.append(dict(
            run_id=key[0], test_id=key[1], prompt=test.prompt if test else "",
            tested_model=model.model_version if model else "?",
            orig_response=o_resp, new_response=n_resp, delta_response=_delta(o_resp, n_resp),
            orig_citation=o_cit, new_citation=n_cit, delta_citation=_delta(o_cit, n_cit),
        ))
    by_model: dict[str, list[dict[str, Any]]] = {}
    for p in pairs:
        by_model.setdefault(p["tested_model"], []).append(p)

    def _agg(items: list[dict[str, Any]]) -> dict[str, Any]:
        both = [p for p in items if p["orig_response"] is not None and p["new_response"] is not None]
        return dict(
            n_pairs=len(items),
            orig_response=_avg([p["orig_response"] for p in items]),
            new_response=_avg([p["new_response"] for p in items]),
            delta_response=_avg([p["delta_response"] for p in items]),
            abs_delta_response=_avg([abs(p["delta_response"]) for p in items if p["delta_response"] is not None]),
            orig_citation=_avg([p["orig_citation"] for p in items]),
            new_citation=_avg([p["new_citation"] for p in items]),
            delta_citation=_avg([p["delta_citation"] for p in items]),
            rank_correlation=agreement.spearman_rho([p["orig_response"] for p in both],
                                                    [p["new_response"] for p in both]) if len(both) >= 3 else None,
        )

    pairs.sort(key=lambda p: (-(abs(p["delta_response"]) if p["delta_response"] is not None else -1), p["run_id"],
                              p["test_id"]))
    return Comparison(
        pairs=pairs[:top],
        by_model=[dict(tested_model=k, **_agg(v)) for k, v in sorted(by_model.items())],
        summary=_agg(pairs),
    )


# ---------------------------------------------------------------------
# Promotion réversible (fin de E8)
# ---------------------------------------------------------------------
_EVAL_COLS = ("test_id", "judge_model_id", "judge_run_index", "response_quality_label", "response_quality_score",
              "citation_quality_label", "citation_quality_score")


@dataclass
class PromotionResult:
    done: list[int] = field(default_factory=list)                    # runs basculés
    skipped: list[tuple[int, str]] = field(default_factory=list)     # (run, motif)


def _copy_to_archive(session: Session, run_id: int, archive_id: int) -> None:
    src = select(literal(archive_id), RunEvaluation.run_id, *[getattr(RunEvaluation, c) for c in _EVAL_COLS]) \
        .where(RunEvaluation.run_id == run_id)
    session.execute(insert(RejudgeEvaluation).from_select(["batch_id", "run_id", *_EVAL_COLS], src))


def _install(session: Session, run_id: int, source_batch_id: int) -> None:
    """Remplace les notes officielles du run par celles d'un lot (rejugé ou archive d'origine)."""
    session.execute(delete(RunEvaluation).where(RunEvaluation.run_id == run_id))
    src = select(RejudgeEvaluation.run_id, *[getattr(RejudgeEvaluation, c) for c in _EVAL_COLS]) \
        .where(RejudgeEvaluation.batch_id == source_batch_id, RejudgeEvaluation.run_id == run_id)
    session.execute(insert(RunEvaluation).from_select(["run_id", *_EVAL_COLS], src))


def promote(session: Session, org: Organization, batch: EvaluationBatch, *, user_id: Optional[int]) -> PromotionResult:
    """Les notes du lot deviennent les notes officielles de ses runs (hors campagne).
    Lève LaunchError : lot pas terminé (409). Les runs non promus sont listés avec leur motif."""
    if batch.organization_id != org.id or batch.kind != "rejudge":
        raise LaunchError("not_found", "Lot de rejugement introuvable.")
    if status(session, batch) != "done":
        raise LaunchError("conflict", "Le lot n'est pas terminé : sa promotion attend la fin du rejugement.")
    with_notes = set(session.execute(
        select(RejudgeEvaluation.run_id).where(RejudgeEvaluation.batch_id == batch.id).distinct()).scalars())
    res = PromotionResult()
    archive: Optional[EvaluationBatch] = None
    for run in session.execute(select(RunRow).where(RunRow.run_id.in_(batch.run_ids or [-1]))
                               .order_by(RunRow.run_id)).scalars():
        if run.campaign_id is not None:
            res.skipped.append((run.run_id, "run de campagne : son protocole fige les notateurs"))
            continue
        if run.run_id not in with_notes:
            res.skipped.append((run.run_id, "aucune note dans ce lot"))
            continue
        if run.reference_batch_id == batch.id:
            res.skipped.append((run.run_id, "déjà promu"))
            continue
        if run.origin_batch_id is None:
            if archive is None:
                archive = EvaluationBatch(
                    organization_id=org.id, kind="origin", run_ids=[], judges=[],
                    label=f"Notes d'origine archivées à la promotion du lot #{batch.id}", created_by=user_id)
                session.add(archive)
                session.flush()
            _copy_to_archive(session, run.run_id, archive.id)
            archive.run_ids = [*archive.run_ids, run.run_id]
            run.origin_batch_id = archive.id
        _install(session, run.run_id, batch.id)
        run.reference_batch_id = batch.id
        res.done.append(run.run_id)
    if res.done:
        batch.promoted_at, batch.promoted_by = datetime.now(timezone.utc), user_id
    session.commit()
    return res


def revert(session: Session, org: Organization, batch: EvaluationBatch) -> list[int]:
    """Restaure les notes d'origine des runs dont ce lot est la référence. Renvoie ces runs."""
    if batch.organization_id != org.id or batch.kind != "rejudge":
        raise LaunchError("not_found", "Lot de rejugement introuvable.")
    restored = []
    for run in session.execute(select(RunRow).where(RunRow.reference_batch_id == batch.id)
                               .order_by(RunRow.run_id)).scalars():
        _install(session, run.run_id, run.origin_batch_id)
        run.reference_batch_id = None
        restored.append(run.run_id)
    batch.promoted_at, batch.promoted_by = None, None
    session.commit()
    return restored


def promoted_runs(session: Session, batch: EvaluationBatch) -> list[int]:
    return list(session.execute(
        select(RunRow.run_id).where(RunRow.reference_batch_id == batch.id).order_by(RunRow.run_id)).scalars())


def judges_label(batch: EvaluationBatch) -> str:
    return ", ".join(f"{j['model_version']}" + (f" ×{j['repeats']}" if j.get("repeats", 1) > 1 else "")
                     for j in batch.judges or [])


def estimate_display(batch: EvaluationBatch) -> Optional[Decimal]:
    return Decimal(batch.estimate_eur).quantize(Decimal("0.01")) if batch.estimate_eur is not None else None
