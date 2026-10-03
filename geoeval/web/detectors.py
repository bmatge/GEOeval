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

Réglages du détecteur `always_wrong` : défaut plateforme, surchargeable par entité et
hérité par son sous-arbre (résolveur du plus proche, champ par champ).
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

from geoeval.db.models import DetectorSetting, LlmContract, Model, Organization, RunEvaluation, RunRow, Test
from geoeval.web import hierarchy, notifications

logger = logging.getLogger("geoeval.web.detectors")

DEFAULT_ALWAYS_WRONG_RUNS = 3
DEFAULT_ALWAYS_WRONG_THRESHOLD = Decimal("5")
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


@dataclass
class AlwaysWrongSettings:
    runs: int
    threshold: Decimal
    runs_from: Optional[Organization] = None        # None = défaut plateforme
    threshold_from: Optional[Organization] = None


def own_settings(session: Session, org_id: int) -> Optional[DetectorSetting]:
    return session.get(DetectorSetting, org_id)


def effective_settings(session: Session, org: Organization) -> AlwaysWrongSettings:
    runs, threshold = platform_defaults()
    eff = AlwaysWrongSettings(runs=runs, threshold=threshold)
    r = hierarchy.resolve_nearest(session, org, lambda s, n: getattr(own_settings(s, n.id), "always_wrong_runs", None))
    if r.value is not None:
        eff.runs, eff.runs_from = int(r.value), r.source
    t = hierarchy.resolve_nearest(session, org,
                                  lambda s, n: getattr(own_settings(s, n.id), "always_wrong_threshold", None))
    if t.value is not None:
        eff.threshold, eff.threshold_from = Decimal(t.value), t.source
    return eff


def set_settings(session: Session, org: Organization, *, runs: Optional[int], threshold: Any,
                 updated_by: Optional[int] = None) -> Optional[DetectorSetting]:
    """Réglages propres de l'entité (None = hériter). Sans aucun réglage, la ligne disparaît."""
    if runs is not None and not 2 <= int(runs) <= 20:
        raise ValueError("Le nombre d'évaluations consécutives doit être compris entre 2 et 20.")
    thr: Optional[Decimal] = None
    if threshold is not None and str(threshold).strip() != "":
        try:
            thr = Decimal(str(threshold).replace(",", ".").strip())
        except InvalidOperation:
            raise ValueError("Seuil invalide : note sur 10 attendue.")
        if not Decimal("0") <= thr <= Decimal("10"):
            raise ValueError("Le seuil doit être compris entre 0 et 10.")
    row = own_settings(session, org.id)
    if runs is None and thr is None:
        if row is not None:
            session.delete(row)
            session.commit()
        return None
    if row is None:
        row = DetectorSetting(organization_id=org.id)
        session.add(row)
    row.always_wrong_runs = int(runs) if runs is not None else None
    row.always_wrong_threshold = thr
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


def after_evaluation_safely(run_id: int) -> int:
    """Hook worker après l'évaluation d'un run (sa propre session, sans exception)."""
    from geoeval.db.session import SessionLocal

    try:
        with SessionLocal() as session:
            return check_always_wrong(session, run_id)
    except Exception:  # noqa: BLE001 — un détecteur ne casse jamais un job
        logger.exception("détecteur « toujours faux » en échec (run=%s)", run_id)
        return 0
