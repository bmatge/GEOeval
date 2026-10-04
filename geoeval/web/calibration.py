"""
Calibration des notateurs (ADR-089 §2.9, suite E8).

Arbitrages : **annotation dans l'application** — les éditeurs d'une entité annotent des
résultats de ses runs (label et note, réponse et citations) ; ces annotations forment
le jeu de calibration de l'entité, **hérité vers le bas** (une sous-entité y ajoute les
siennes). Le gold set importé par la plateforme (CSV, ADR-079, `organization_id` NULL)
compte pour toutes les entités. **Indicateur + notification** — l'accord de chaque
notateur avec ce jeu est suivi globalement et par thème ; sous un seuil hérité, une
notification part (`detectors.check_judge_disagreement`). Rien n'est bloqué.

Une « version de notateur » est un modèle (`model_version`) pour les notes d'origine,
ou un couple (lot de rejugement, modèle) pour les notes rejugées : un nouveau notateur
se calibre en rejugeant des runs déjà annotés.

Métrique de référence : corrélation de rang (Spearman) sur la note de réponse ; le
kappa de Cohen (labels) et l'écart absolu moyen sont affichés à côté.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from geoeval.db.models import (
    EvaluationBatch,
    GoldAnnotation,
    Model,
    Organization,
    QuestionTheme,
    RejudgeEvaluation,
    RunEvaluation,
    RunResult,
    RunRow,
    Theme,
)
from geoeval.web import agreement, gold, hierarchy

MAX_NOTES = 2000


class CalibrationError(ValueError):
    def __init__(self, detail: str, status: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status


# ---------------------------------------------------------------------
# Annotation dans l'application
# ---------------------------------------------------------------------
def annotate(
    session: Session, org: Organization, *, run_id: int, test_id: int, user_id: Optional[int], email: str,
    response_label: str, response_score: Any, citation_label: str, citation_score: Any, notes: str = "",
) -> GoldAnnotation:
    """Annotation (ou mise à jour) d'un résultat d'un run de l'entité par un utilisateur."""
    run = session.get(RunRow, run_id)
    if run is None or run.organization_id != org.id:
        raise CalibrationError("Run introuvable.", 404)
    if session.get(RunResult, (run_id, test_id)) is None:
        raise CalibrationError("Cette question ne figure pas dans ce run.", 404)
    email = (email or "").strip().lower()
    if not email:
        raise CalibrationError("Annotateur inconnu (email requis).", 403)
    try:
        payload = dict(
            test_id=test_id, run_id=run_id, annotator_email=email, organization_id=org.id,
            annotator_user_id=user_id,
            response_label=gold._validate_label(response_label, "Label de réponse"),
            response_score=gold._validate_score(response_score),
            citation_label=gold._validate_label(citation_label, "Label de citations"),
            citation_score=gold._validate_score(citation_score),
            notes=(notes or "").strip()[:MAX_NOTES] or None,
        )
    except ValueError as e:
        raise CalibrationError(str(e))
    ins = insert(GoldAnnotation).values(**payload)
    session.execute(ins.on_conflict_do_update(
        index_elements=[GoldAnnotation.test_id, GoldAnnotation.run_id, GoldAnnotation.annotator_email],
        set_={k: getattr(ins.excluded, k) for k in (
            "organization_id", "annotator_user_id", "response_label", "response_score", "citation_label",
            "citation_score", "notes")},
    ))
    session.commit()
    return session.execute(select(GoldAnnotation).where(
        GoldAnnotation.test_id == test_id, GoldAnnotation.run_id == run_id, GoldAnnotation.annotator_email == email,
    )).scalar_one()


def annotation_of(session: Session, run_id: int, test_id: int, email: str) -> Optional[GoldAnnotation]:
    return session.execute(select(GoldAnnotation).where(
        GoldAnnotation.run_id == run_id, GoldAnnotation.test_id == test_id,
        GoldAnnotation.annotator_email == (email or "").strip().lower(),
    )).scalar_one_or_none()


def annotated_test_ids(session: Session, run_id: int) -> dict[int, int]:
    """test_id → nombre d'annotations sur ce run (pour l'affichage du détail d'un run)."""
    return {t: int(n) for t, n in session.execute(
        select(GoldAnnotation.test_id, func.count()).where(GoldAnnotation.run_id == run_id)
        .group_by(GoldAnnotation.test_id)
    ).all()}


def scope_ids(session: Session, org: Organization) -> list[int]:
    """Entités dont les annotations comptent pour `org` : elle-même et ses ancêtres."""
    return [o.id for o in hierarchy.chain(session, org)]


def _scope_clause(session: Session, org: Organization):
    return or_(GoldAnnotation.organization_id.in_(scope_ids(session, org)), GoldAnnotation.organization_id.is_(None))


def list_annotations(session: Session, org: Organization, *, own_only: bool = False, limit: int = 500) -> list[GoldAnnotation]:
    clause = GoldAnnotation.organization_id == org.id if own_only else _scope_clause(session, org)
    return list(session.execute(
        select(GoldAnnotation).where(clause).order_by(GoldAnnotation.annotated_at.desc()).limit(limit)
    ).scalars())


# ---------------------------------------------------------------------
# Accord notateur / gold
# ---------------------------------------------------------------------
def _mode(labels: list[Optional[str]]) -> str:
    vals = [label for label in labels if label]
    return Counter(sorted(vals)).most_common(1)[0][0] if vals else ""


def _collapse(rows) -> dict[tuple[int, int], dict[str, Any]]:
    """(run, test) → moyenne des notes et label majoritaire (plusieurs annotateurs ou répétitions)."""
    acc: dict[tuple[int, int], dict[str, list]] = {}
    for run_id, test_id, rl, rs, cl, cs in rows:
        a = acc.setdefault((run_id, test_id), {"rl": [], "rs": [], "cl": [], "cs": []})
        a["rl"].append(rl)
        a["cl"].append(cl)
        if rs is not None:
            a["rs"].append(float(rs))
        if cs is not None:
            a["cs"].append(float(cs))
    return {k: dict(response_label=_mode(v["rl"]), citation_label=_mode(v["cl"]),
                    response_score=sum(v["rs"]) / len(v["rs"]) if v["rs"] else None,
                    citation_score=sum(v["cs"]) / len(v["cs"]) if v["cs"] else None)
            for k, v in acc.items()}


def gold_pairs(session: Session, org: Organization) -> dict[tuple[int, int], dict[str, Any]]:
    rows = session.execute(
        select(GoldAnnotation.run_id, GoldAnnotation.test_id, GoldAnnotation.response_label,
               GoldAnnotation.response_score, GoldAnnotation.citation_label, GoldAnnotation.citation_score)
        .where(_scope_clause(session, org))
    ).all()
    return _collapse(rows)


def _judge_pairs(session: Session, pairs: set[tuple[int, int]], model_id: int,
                 batch_id: Optional[int]) -> dict[tuple[int, int], dict[str, Any]]:
    if not pairs:
        return {}
    m = RejudgeEvaluation if batch_id is not None else RunEvaluation
    extra = [RejudgeEvaluation.batch_id == batch_id] if batch_id is not None else []
    run_ids = {r for r, _ in pairs}
    rows = session.execute(
        select(m.run_id, m.test_id, m.response_quality_label, m.response_quality_score,
               m.citation_quality_label, m.citation_quality_score)
        .where(m.judge_model_id == model_id, m.run_id.in_(run_ids), *extra)
    ).all()
    return {k: v for k, v in _collapse(rows).items() if k in pairs}


def metrics(gold_vals: list[dict[str, Any]], judge_vals: list[dict[str, Any]]) -> dict[str, Any]:
    """Accord sur des paires alignées (même ordre)."""
    n = len(gold_vals)
    resp = [(g["response_score"], j["response_score"]) for g, j in zip(gold_vals, judge_vals)
            if g["response_score"] is not None and j["response_score"] is not None]
    cit = [(g["citation_score"], j["citation_score"]) for g, j in zip(gold_vals, judge_vals)
           if g["citation_score"] is not None and j["citation_score"] is not None]
    return dict(
        n_pairs=n,
        response_spearman=agreement.spearman_rho([a for a, _ in resp], [b for _, b in resp]),
        response_kappa=agreement.cohen_kappa([g["response_label"] for g in gold_vals],
                                             [j["response_label"] for j in judge_vals]) if n else None,
        response_mae=sum(abs(a - b) for a, b in resp) / len(resp) if resp else None,
        citation_spearman=agreement.spearman_rho([a for a, _ in cit], [b for _, b in cit]),
        citation_kappa=agreement.cohen_kappa([g["citation_label"] for g in gold_vals],
                                             [j["citation_label"] for j in judge_vals]) if n else None,
    )


@dataclass
class JudgeAgreement:
    model: Model
    batch: Optional[EvaluationBatch]
    overall: dict[str, Any]
    by_theme: list[dict[str, Any]] = field(default_factory=list)   # [{theme_id, label, **metrics}]

    @property
    def key(self) -> str:
        return f"{'batch-' + str(self.batch.id) if self.batch else 'orig'}:{self.model.model_id}"


def agreement_for(session: Session, org: Organization, *, model_id: int, batch_id: Optional[int] = None,
                  gold: Optional[dict] = None) -> Optional[JudgeAgreement]:
    """Accord d'une version de notateur avec le jeu de calibration de l'entité (None sans paire)."""
    gold = gold if gold is not None else gold_pairs(session, org)
    judged = _judge_pairs(session, set(gold), model_id, batch_id)
    keys = sorted(judged)
    if not keys:
        return None
    model = session.get(Model, model_id)
    batch = session.get(EvaluationBatch, batch_id) if batch_id is not None else None
    themes: dict[int, list[int]] = {}
    for test_id, theme_id in session.execute(
        select(QuestionTheme.test_id, QuestionTheme.theme_id).where(QuestionTheme.test_id.in_({t for _, t in keys}))
    ).all():
        themes.setdefault(test_id, []).append(theme_id)
    labels = {t.id: t.label for t in session.execute(
        select(Theme).where(Theme.id.in_({i for ids in themes.values() for i in ids} or {-1}))).scalars()}
    by_theme = []
    for theme_id, label in sorted(labels.items(), key=lambda kv: kv[1]):
        sub = [k for k in keys if theme_id in themes.get(k[1], [])]
        by_theme.append(dict(theme_id=theme_id, label=label,
                             **metrics([gold[k] for k in sub], [judged[k] for k in sub])))
    return JudgeAgreement(model=model, batch=batch, overall=metrics([gold[k] for k in keys], [judged[k] for k in keys]),
                          by_theme=by_theme)


def overview(session: Session, org: Organization) -> list[JudgeAgreement]:
    """Toutes les versions de notateur ayant noté au moins une paire du jeu de calibration :
    notes d'origine par modèle, puis lots de rejugement de l'entité."""
    gold = gold_pairs(session, org)
    if not gold:
        return []
    run_ids = {r for r, _ in gold}
    out: list[JudgeAgreement] = []
    for model_id in session.execute(
        select(RunEvaluation.judge_model_id).where(RunEvaluation.run_id.in_(run_ids)).distinct()
    ).scalars():
        ag = agreement_for(session, org, model_id=model_id, gold=gold)
        if ag is not None:
            out.append(ag)
    for batch_id, model_id in session.execute(
        select(RejudgeEvaluation.batch_id, RejudgeEvaluation.judge_model_id)
        .join(EvaluationBatch, EvaluationBatch.id == RejudgeEvaluation.batch_id)
        .where(RejudgeEvaluation.run_id.in_(run_ids), EvaluationBatch.organization_id.in_(scope_ids(session, org)),
               EvaluationBatch.kind == "rejudge")
        .distinct()
    ).all():
        ag = agreement_for(session, org, model_id=model_id, batch_id=batch_id, gold=gold)
        if ag is not None:
            out.append(ag)
    out.sort(key=lambda a: (a.batch.id if a.batch else 0, a.model.model_version))
    return out


def fmt(value: Optional[float]) -> str:
    return "—" if value is None else f"{Decimal(str(value)).quantize(Decimal('0.01'))}"
