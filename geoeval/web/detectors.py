"""
Détecteurs d'événements (ADR-089 §2.8, chantier E7).

Ils tournent dans le worker (fin de job, planificateur) et n'écrivent que des
notifications : ils ne modifient jamais un résultat d'évaluation.

- `job_failed`        : une évaluation s'arrête sur une erreur (couvre le notateur indisponible).
- `contract_expiring` : un contrat actif arrive à échéance (J-30, puis J-7) ;
  `contract_expired`  : il est échu (ses appels sont bloqués, E5).
- `always_wrong`      : pour un couple (question, IA évaluée) d'une entité, les N
  dernières évaluations sont sous le seuil (moyenne des notateurs, note de réponse
  sur 10). Une seule alerte par série : la clé porte le premier run de la série,
  qui ne change pas tant que la série continue.

- `citation_drop`     : la part des citations officielles d'un run (domaines du périmètre)
  chute d'au moins N points sous la moyenne des 3 runs précédents (même entité, même
  périmètre, même IA évaluée). Une alerte par run.

- `judge_disagreement` : sur le jeu de calibration de l'entité (annotations humaines,
  héritées), la corrélation de rang notateur / gold (note de réponse) passe sous un seuil,
  globalement ou pour un thème, avec assez de paires. Au plus une alerte par semaine et
  par (version de notateur, thème).

Réglages des détecteurs : défaut plateforme, surchargeable par entité et hérité par son
sous-arbre (résolveur du plus proche, champ par champ).
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from geoeval.db.models import (
    DetectorSetting,
    LlmContract,
    Model,
    Organization,
    Perimeter,
    RunEvaluation,
    RunResult,
    RunRow,
    Test,
)
from geoeval.web import hierarchy, notifications
from geoeval.web import perimeters as perimeters_svc

logger = logging.getLogger("geoeval.web.detectors")

DEFAULT_ALWAYS_WRONG_RUNS = 3
DEFAULT_ALWAYS_WRONG_THRESHOLD = Decimal("5")
DEFAULT_CITATION_DROP_POINTS = Decimal("20")
DEFAULT_CALIBRATION_MIN_RHO = Decimal("0.5")
DEFAULT_CALIBRATION_MIN_PAIRS = 10
CITATION_DROP_BASELINE_RUNS = 3
CONTRACT_NOTICE_DAYS = (30, 7)


# ---------------------------------------------------------------------
# Réglages hérités
# ---------------------------------------------------------------------
def platform_defaults() -> tuple[int, Decimal]:
    """Défauts plateforme (surchargeables par GEOEVAL_ALWAYS_WRONG_RUNS / _THRESHOLD)."""
    runs, threshold = DEFAULT_ALWAYS_WRONG_RUNS, DEFAULT_ALWAYS_WRONG_THRESHOLD
    try:
        raw = (os.environ.get("GEOEVAL_ALWAYS_WRONG_RUNS") or "").strip()
        if raw:
            runs = max(2, min(20, int(raw)))
        raw = (os.environ.get("GEOEVAL_ALWAYS_WRONG_THRESHOLD") or "").strip()
        if raw:
            threshold = max(Decimal("0"), min(Decimal("10"), Decimal(raw.replace(",", "."))))
    except (ValueError, InvalidOperation):
        logger.warning("réglage du détecteur « toujours faux » invalide dans l'environnement : défauts conservés")
    return runs, threshold


def citation_drop_default() -> Decimal:
    """Écart (en points de pourcentage) déclenchant `citation_drop` (GEOEVAL_CITATION_DROP_POINTS)."""
    raw = (os.environ.get("GEOEVAL_CITATION_DROP_POINTS") or "").strip()
    if not raw:
        return DEFAULT_CITATION_DROP_POINTS
    try:
        return max(Decimal("1"), min(Decimal("100"), Decimal(raw.replace(",", "."))))
    except InvalidOperation:
        logger.warning("GEOEVAL_CITATION_DROP_POINTS invalide : défaut conservé")
        return DEFAULT_CITATION_DROP_POINTS


def calibration_defaults() -> tuple[Decimal, int]:
    """Seuil de corrélation (GEOEVAL_CALIBRATION_MIN_RHO, 0 à 1) et nombre minimal de
    paires annotées pour juger un notateur (GEOEVAL_CALIBRATION_MIN_PAIRS, au moins 3)."""
    rho, pairs = DEFAULT_CALIBRATION_MIN_RHO, DEFAULT_CALIBRATION_MIN_PAIRS
    try:
        raw = (os.environ.get("GEOEVAL_CALIBRATION_MIN_RHO") or "").strip()
        if raw:
            rho = max(Decimal("0"), min(Decimal("1"), Decimal(raw.replace(",", "."))))
        raw = (os.environ.get("GEOEVAL_CALIBRATION_MIN_PAIRS") or "").strip()
        if raw:
            pairs = max(3, int(raw))
    except (ValueError, InvalidOperation):
        logger.warning("réglage de calibration invalide dans l'environnement : défauts conservés")
    return rho, pairs


@dataclass
class AlwaysWrongSettings:
    """Réglages effectifs des détecteurs (nom historique : `always_wrong` en premier)."""
    runs: int
    threshold: Decimal
    citation_drop_points: Decimal = DEFAULT_CITATION_DROP_POINTS
    calibration_min_rho: Decimal = DEFAULT_CALIBRATION_MIN_RHO
    runs_from: Optional[Organization] = None        # None = défaut plateforme
    threshold_from: Optional[Organization] = None
    citation_drop_from: Optional[Organization] = None
    calibration_from: Optional[Organization] = None


def own_settings(session: Session, org_id: int) -> Optional[DetectorSetting]:
    return session.get(DetectorSetting, org_id)


def effective_settings(session: Session, org: Organization) -> AlwaysWrongSettings:
    runs, threshold = platform_defaults()
    eff = AlwaysWrongSettings(runs=runs, threshold=threshold, citation_drop_points=citation_drop_default(),
                              calibration_min_rho=calibration_defaults()[0])
    r = hierarchy.resolve_nearest(session, org, lambda s, n: getattr(own_settings(s, n.id), "always_wrong_runs", None))
    if r.value is not None:
        eff.runs, eff.runs_from = int(r.value), r.source
    t = hierarchy.resolve_nearest(session, org,
                                  lambda s, n: getattr(own_settings(s, n.id), "always_wrong_threshold", None))
    if t.value is not None:
        eff.threshold, eff.threshold_from = Decimal(t.value), t.source
    c = hierarchy.resolve_nearest(session, org,
                                  lambda s, n: getattr(own_settings(s, n.id), "citation_drop_points", None))
    if c.value is not None:
        eff.citation_drop_points, eff.citation_drop_from = Decimal(c.value), c.source
    k = hierarchy.resolve_nearest(session, org,
                                  lambda s, n: getattr(own_settings(s, n.id), "calibration_min_rho", None))
    if k.value is not None:
        eff.calibration_min_rho, eff.calibration_from = Decimal(k.value), k.source
    return eff


def _decimal(raw: Any, error: str) -> Optional[Decimal]:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return Decimal(str(raw).replace(",", ".").strip())
    except InvalidOperation:
        raise ValueError(error)


def set_settings(session: Session, org: Organization, *, runs: Optional[int], threshold: Any,
                 citation_drop_points: Any = None, calibration_min_rho: Any = None,
                 updated_by: Optional[int] = None) -> Optional[DetectorSetting]:
    """Remplace les réglages propres de l'entité (None = hériter). Sans aucun réglage, la ligne disparaît."""
    if runs is not None and not 2 <= int(runs) <= 20:
        raise ValueError("Le nombre d'évaluations consécutives doit être compris entre 2 et 20.")
    thr = _decimal(threshold, "Seuil invalide : note sur 10 attendue.")
    if thr is not None and not Decimal("0") <= thr <= Decimal("10"):
        raise ValueError("Le seuil doit être compris entre 0 et 10.")
    drop = _decimal(citation_drop_points, "Écart invalide : nombre de points attendu.")
    if drop is not None and not Decimal("0") < drop <= Decimal("100"):
        raise ValueError("L'écart de citations officielles doit être compris entre 0 (exclu) et 100 points.")
    rho = _decimal(calibration_min_rho, "Seuil de calibration invalide : corrélation entre 0 et 1 attendue.")
    if rho is not None and not Decimal("0") <= rho <= Decimal("1"):
        raise ValueError("Le seuil de calibration doit être compris entre 0 et 1.")
    row = own_settings(session, org.id)
    if runs is None and thr is None and drop is None and rho is None:
        if row is not None:
            session.delete(row)
            session.commit()
        return None
    if row is None:
        row = DetectorSetting(organization_id=org.id)
        session.add(row)
    row.always_wrong_runs = int(runs) if runs is not None else None
    row.always_wrong_threshold = thr
    row.citation_drop_points = drop
    row.calibration_min_rho = rho
    row.updated_by = updated_by
    session.commit()
    return row


# ---------------------------------------------------------------------
# job_failed
# ---------------------------------------------------------------------
def job_failed(org_id: Optional[int], job_id: str, error: str) -> int:
    """Notifie l'échec d'une évaluation (sa propre session, sans exception)."""
    detail = (error or "erreur inconnue").strip()
    if len(detail) > 500:
        detail = detail[:500] + "…"
    org_slug = _slug(org_id)
    return notifications.notify_safely(
        org_id, "job_failed",
        title="Évaluation en échec",
        body=f"L'évaluation {job_id[:8]} s'est arrêtée : {detail}",
        link=f"/o/{org_slug}/jobs/{job_id}" if org_slug else None,
        payload={"job_id": job_id, "error": detail}, dedup_key=f"job_failed:{job_id}",
    )


def _slug(org_id: Optional[int]) -> Optional[str]:
    if org_id is None:
        return None
    from geoeval.db.session import SessionLocal

    try:
        with SessionLocal() as session:
            org = session.get(Organization, org_id)
            return org.slug if org else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------
# Contrats expirants / expirés
# ---------------------------------------------------------------------
def check_contracts(session: Session, *, today: Optional[date] = None) -> int:
    """Contrats actifs échéant dans 30 / 7 jours, ou échus. Idempotent (clé par contrat,
    date de fin et palier) : peut tourner à chaque tick. Renvoie le nombre de notifications."""
    today = today or date.today()
    horizon = today + timedelta(days=max(CONTRACT_NOTICE_DAYS))
    rows = session.execute(
        select(LlmContract, Organization)
        .join(Organization, Organization.id == LlmContract.organization_id)
        .where(LlmContract.is_active.is_(True), LlmContract.valid_to.is_not(None), LlmContract.valid_to <= horizon)
    ).all()
    sent = 0
    for c, org in rows:
        days = (c.valid_to - today).days
        link = f"/o/{org.slug}/contracts"
        if days < 0:
            sent += len(notifications.notify(
                session, org, "contract_expired",
                title=f"Contrat LLM expiré — {c.label}",
                body=(f"Le contrat « {c.label} » de « {org.name} » est échu depuis le {c.valid_to:%d/%m/%Y}. "
                      "Ses appels sont bloqués (pas de repli sur une autre clé) : renouvelle-le, ou désactive-le "
                      "pour revenir à la clé de la plateforme."),
                link=link, payload={"contract_id": c.id, "valid_to": c.valid_to.isoformat()},
                dedup_key=f"contract_expired:{c.id}:{c.valid_to.isoformat()}",
            ).created)
            continue
        tier = min((d for d in CONTRACT_NOTICE_DAYS if days <= d), default=None)
        if tier is None:
            continue
        sent += len(notifications.notify(
            session, org, "contract_expiring",
            title=f"Contrat LLM expirant dans {days} jour(s) — {c.label}",
            body=(f"Le contrat « {c.label} » de « {org.name} » prend fin le {c.valid_to:%d/%m/%Y}. "
                  "Après cette date, ses appels seront bloqués."),
            link=link, payload={"contract_id": c.id, "valid_to": c.valid_to.isoformat(), "days": days},
            dedup_key=f"contract_expiring:{c.id}:{c.valid_to.isoformat()}:{tier}",
        ).created)
    return sent


_last_contract_check = 0.0
CONTRACT_CHECK_INTERVAL_S = 3600


def check_contracts_throttled(session: Session) -> int:
    """Au plus une fois par heure et par processus (le planificateur tourne chaque minute)."""
    global _last_contract_check
    now = time.monotonic()
    if _last_contract_check and now - _last_contract_check < CONTRACT_CHECK_INTERVAL_S:
        return 0
    _last_contract_check = now
    try:
        return check_contracts(session)
    except Exception:  # noqa: BLE001 — un détecteur ne casse jamais le planificateur
        logger.exception("vérification des contrats expirants en échec")
        session.rollback()
        return 0


# ---------------------------------------------------------------------
# always_wrong
# ---------------------------------------------------------------------
HISTORY_LIMIT = 50


def streak(scores: list[tuple[int, Decimal]], threshold: Decimal) -> list[int]:
    """`scores` : (run_id, moyenne) du plus récent au plus ancien. Renvoie les run_id de
    la série en cours sous le seuil (du plus récent au plus ancien)."""
    out: list[int] = []
    for run_id, avg in scores:
        if avg is None or Decimal(avg) >= threshold:
            break
        out.append(run_id)
    return out


def check_always_wrong(session: Session, run_id: int) -> int:
    """Après l'évaluation d'un run : pour chaque question du run, regarde la série en
    cours pour (entité, question, IA évaluée). Renvoie le nombre de notifications."""
    run = session.get(RunRow, run_id)
    if run is None:
        return 0
    org = session.get(Organization, run.organization_id)
    model = session.get(Model, run.tested_model_id)
    if org is None or model is None:
        return 0
    cfg = effective_settings(session, org)
    test_ids = list(session.execute(
        select(RunEvaluation.test_id).where(RunEvaluation.run_id == run_id).distinct()
    ).scalars())
    sent = 0
    for test_id in test_ids:
        rows = session.execute(
            select(RunEvaluation.run_id, func.avg(RunEvaluation.response_quality_score))
            .join(RunRow, RunRow.run_id == RunEvaluation.run_id)
            .where(RunRow.organization_id == org.id, RunRow.tested_model_id == model.model_id,
                   RunEvaluation.test_id == test_id, RunEvaluation.run_id <= run_id,
                   RunEvaluation.response_quality_score.is_not(None))
            .group_by(RunEvaluation.run_id).order_by(RunEvaluation.run_id.desc()).limit(HISTORY_LIMIT)
        ).all()
        if not rows or rows[0][0] != run_id:
            continue  # pas de note exploitable pour ce run
        current = streak([(r, a) for r, a in rows], cfg.threshold)
        if len(current) < cfg.runs:
            continue
        test = session.get(Test, test_id)
        prompt = (test.prompt if test else f"#{test_id}")
        excerpt = prompt if len(prompt) <= 120 else prompt[:117] + "…"
        avgs = ", ".join(f"{Decimal(a):.1f}" for _, a in rows[:len(current)])
        sent += len(notifications.notify(
            session, org, "always_wrong",
            title=f"Question toujours fausse pour {model.model_version}",
            body=(f"« {excerpt} » reste sous {cfg.threshold}/10 sur les {len(current)} dernières évaluations "
                  f"de {model.model_version} (moyennes : {avgs}). Vérifie la réponse attendue, ou signale "
                  "l'écart au fournisseur de la question."),
            link=f"/o/{org.slug}/runs/{run_id}",
            payload={"test_id": test_id, "model_id": model.model_id, "run_ids": current,
                     "threshold": str(cfg.threshold), "runs": cfg.runs},
            dedup_key=f"always_wrong:{org.id}:{test_id}:{model.model_id}:{current[-1]}",
        ).created)
    return sent


# ---------------------------------------------------------------------
# citation_drop
# ---------------------------------------------------------------------
def run_official_share(session: Session, run_id: int, domains: list[str]) -> Optional[float]:
    """Part des citations officielles d'un run (toutes questions confondues), None sans citation."""
    from geoeval.web.services import _citation_url

    urls: list[str] = []
    for citations in session.execute(select(RunResult.raw_citations).where(RunResult.run_id == run_id)).scalars():
        urls.extend(u for u in (_citation_url(c) for c in (citations or [])) if u)
    return perimeters_svc.official_share(urls, domains)


def citation_drop(current: Optional[float], previous: list[Optional[float]],
                  points: Decimal) -> Optional[tuple[float, float]]:
    """(moyenne de référence, écart en points) si la chute atteint `points`, sinon None.
    Exige `CITATION_DROP_BASELINE_RUNS` parts précédentes exploitables."""
    usable = [p for p in previous if p is not None]
    if current is None or len(usable) < CITATION_DROP_BASELINE_RUNS:
        return None
    baseline = sum(usable[:CITATION_DROP_BASELINE_RUNS]) / CITATION_DROP_BASELINE_RUNS
    gap = round((baseline - current) * 100, 6)  # 0.7 - 0.5 → 19.999… : arrondi avant comparaison
    return (baseline, gap) if gap >= float(points) else None


def check_citation_drop(session: Session, run_id: int) -> int:
    """Après l'évaluation d'un run rattaché à un périmètre doté de domaines officiels :
    compare sa part de citations officielles à la moyenne des 3 runs précédents
    (même entité, périmètre, IA évaluée). Renvoie le nombre de notifications."""
    run = session.get(RunRow, run_id)
    if run is None or run.perimeter_id is None:
        return 0
    peri = session.get(Perimeter, run.perimeter_id)
    domains = list(peri.domains or []) if peri is not None else []
    if not domains:
        return 0
    org = session.get(Organization, run.organization_id)
    model = session.get(Model, run.tested_model_id)
    if org is None or model is None:
        return 0
    current = run_official_share(session, run_id, domains)
    if current is None:
        return 0
    previous_ids = list(session.execute(
        select(RunRow.run_id)
        .where(RunRow.organization_id == org.id, RunRow.perimeter_id == peri.id,
               RunRow.tested_model_id == model.model_id, RunRow.run_id < run_id)
        .order_by(RunRow.run_id.desc()).limit(HISTORY_LIMIT)
    ).scalars())
    previous: list[float] = []
    for rid in previous_ids:  # les runs sans citation ne comptent pas dans la référence
        share = run_official_share(session, rid, domains)
        if share is not None:
            previous.append(share)
        if len(previous) >= CITATION_DROP_BASELINE_RUNS:
            break
    cfg = effective_settings(session, org)
    hit = citation_drop(current, previous, cfg.citation_drop_points)
    if hit is None:
        return 0
    baseline, gap = hit
    return len(notifications.notify(
        session, org, "citation_drop",
        title=f"Chute des citations officielles — {peri.name} · {model.model_version}",
        body=(f"Dans le run #{run_id}, {current:.0%} des citations de {model.model_version} pointent vers les "
              f"domaines officiels de « {peri.name} », contre {baseline:.0%} en moyenne sur les "
              f"{CITATION_DROP_BASELINE_RUNS} runs précédents ({gap:.0f} points de moins ; seuil "
              f"{cfg.citation_drop_points:g})."),
        link=f"/o/{org.slug}/runs/{run_id}",
        payload={"run_id": run_id, "perimeter_id": peri.id, "model_id": model.model_id,
                 "share": round(current, 4), "baseline": round(baseline, 4), "gap_points": round(gap, 1),
                 "threshold_points": str(cfg.citation_drop_points)},
        dedup_key=f"citation_drop:{run_id}",
    ).created)


def after_evaluation_safely(run_id: int) -> int:
    """Hook worker après l'évaluation d'un run (sa propre session, sans exception).
    Chaque détecteur est isolé : l'échec de l'un n'empêche pas l'autre."""
    from geoeval.db.session import SessionLocal

    sent = 0
    for name, check in (("toujours faux", check_always_wrong), ("chute des citations", check_citation_drop)):
        try:
            with SessionLocal() as session:
                sent += check(session, run_id)
        except Exception:  # noqa: BLE001 — un détecteur ne casse jamais un job
            logger.exception("détecteur « %s » en échec (run=%s)", name, run_id)
    return sent


# ---------------------------------------------------------------------
# judge_disagreement (E8 suite)
# ---------------------------------------------------------------------
def disagreements(overall_and_themes: list[tuple[Optional[int], str, dict[str, Any]]], min_rho: Decimal,
                  min_pairs: int) -> list[tuple[Optional[int], str, float, int]]:
    """(theme_id, libellé, rho, n) sous le seuil, parmi les portées ayant assez de paires."""
    out = []
    for theme_id, label, m in overall_and_themes:
        rho = m.get("response_spearman")
        if m.get("n_pairs", 0) >= min_pairs and rho is not None and rho < float(min_rho):
            out.append((theme_id, label, rho, m["n_pairs"]))
    return out


def check_judge_disagreement(session: Session, org: Organization, model_ids: list[int], *,
                             batch_id: Optional[int] = None, today: Optional[date] = None) -> int:
    """Accord de chaque version de notateur avec le jeu de calibration de l'entité ;
    notifie sous le seuil. Renvoie le nombre de notifications."""
    from geoeval.web import calibration

    cfg = effective_settings(session, org)
    _, min_pairs = calibration_defaults()
    gold = calibration.gold_pairs(session, org)
    if len(gold) < min_pairs:
        return 0
    week = (today or date.today()).isocalendar()
    sent = 0
    for model_id in dict.fromkeys(model_ids):
        ag = calibration.agreement_for(session, org, model_id=model_id, batch_id=batch_id, gold=gold)
        if ag is None:
            continue
        scopes = [(None, "tous thèmes", ag.overall)] + [(t["theme_id"], t["label"], t) for t in ag.by_theme]
        source = f"lot #{batch_id} · " if batch_id is not None else ""
        for theme_id, label, rho, n in disagreements(scopes, cfg.calibration_min_rho, min_pairs):
            sent += len(notifications.notify(
                session, org, "judge_disagreement",
                title=f"Notateur en désaccord avec le gold — {source}{ag.model.model_version} ({label})",
                body=(f"Sur {n} paires annotées ({label}), la corrélation de rang entre {ag.model.model_version} "
                      f"et les annotations humaines est de {rho:.2f}, sous le seuil de {cfg.calibration_min_rho}. "
                      "Ses notes sont à lire avec prudence ; un rejugement avec un autre notateur permet de comparer."),
                link=f"/o/{org.slug}/calibration",
                payload={"model_id": model_id, "batch_id": batch_id, "theme_id": theme_id, "rho": round(rho, 3),
                         "n_pairs": n, "threshold": str(cfg.calibration_min_rho)},
                dedup_key=(f"judge_disagreement:{org.id}:{batch_id or 'orig'}:{model_id}:{theme_id or 'all'}:"
                           f"{week[0]}-W{week[1]:02d}"),
            ).created)
    return sent


def calibration_after_evaluation_safely(org_id: Optional[int], judges: list[Any], *,
                                        batch_id: Optional[int] = None) -> int:
    """Hook worker : `judges` = versions (str) ou model_id (int). Sa propre session, sans exception."""
    if org_id is None or not judges:
        return 0
    from geoeval.db.session import SessionLocal

    try:
        with SessionLocal() as session:
            org = session.get(Organization, org_id)
            if org is None:
                return 0
            ids = [j for j in judges if isinstance(j, int)]
            versions = [j for j in judges if isinstance(j, str)]
            if versions:
                ids += list(session.execute(select(Model.model_id).where(Model.model_version.in_(versions))).scalars())
            return check_judge_disagreement(session, org, ids, batch_id=batch_id)
    except Exception:  # noqa: BLE001 — un détecteur ne casse jamais un job
        logger.exception("détecteur de calibration en échec (org=%s, lot=%s)", org_id, batch_id)
        return 0
