"""
Métriques d'accord juge / annotateurs (ADR-079 §5).

Calculs à la main, zéro dépendance (numpy/pandas volontairement évités —
POC + pas d'ajout de dépendance).
"""
from __future__ import annotations

import math
from decimal import Decimal
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from evaluate import CONFORMITY_LABELS
from models import GoldAnnotation, RunEvaluation


def cohen_kappa(labels_a: Sequence[str], labels_b: Sequence[str]) -> Optional[float]:
    """Kappa de Cohen pour deux séquences de labels catégoriels.

    Renvoie None si les listes sont vides / non alignées. Vocabulaire = union
    des labels observés.
    """
    if not labels_a or len(labels_a) != len(labels_b):
        return None
    n = len(labels_a)
    labels = sorted(set(labels_a) | set(labels_b))
    if len(labels) < 2:
        return 1.0  # accord trivial (une seule catégorie)

    # p_o : accord observé
    agree = sum(1 for a, b in zip(labels_a, labels_b) if a == b)
    p_o = agree / n

    # p_e : accord attendu par hasard
    p_e = 0.0
    for lab in labels:
        pa = sum(1 for a in labels_a if a == lab) / n
        pb = sum(1 for b in labels_b if b == lab) / n
        p_e += pa * pb

    if p_e >= 1.0:
        return 1.0
    return (p_o - p_e) / (1.0 - p_e)


def spearman_rho(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    """Corrélation de Spearman (rang) pour deux séries numériques."""
    if not a or len(a) != len(b):
        return None
    if len(a) < 2:
        return None

    def _ranks(xs: Sequence[float]) -> list[float]:
        indexed = sorted(range(len(xs)), key=lambda i: xs[i])
        r = [0.0] * len(xs)
        i = 0
        while i < len(indexed):
            j = i
            while j + 1 < len(indexed) and xs[indexed[j + 1]] == xs[indexed[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0  # rangs 1-based, moyenne pour égalités
            for k in range(i, j + 1):
                r[indexed[k]] = avg
            i = j + 1
        return r

    ra, rb = _ranks(a), _ranks(b)
    n = len(a)
    mean_a = sum(ra) / n
    mean_b = sum(rb) / n
    num = sum((ra[i] - mean_a) * (rb[i] - mean_b) for i in range(n))
    da = math.sqrt(sum((ra[i] - mean_a) ** 2 for i in range(n)))
    db = math.sqrt(sum((rb[i] - mean_b) ** 2 for i in range(n)))
    if da == 0 or db == 0:
        return None
    return num / (da * db)


# =====================================================================
# Comparaison juge vs gold set
# =====================================================================
def compute_agreement_vs_gold(
    session: Session, judge_model_id: int
) -> dict[str, Any]:
    """Compare tous les jugements du judge_model aux annotations gold pour les
    mêmes (test_id, run_id)."""
    stmt = (
        select(
            GoldAnnotation.response_label, RunEvaluation.response_quality_label,
            GoldAnnotation.response_score, RunEvaluation.response_quality_score,
            GoldAnnotation.citation_label, RunEvaluation.citation_quality_label,
            GoldAnnotation.citation_score, RunEvaluation.citation_quality_score,
        )
        .select_from(GoldAnnotation)
        .join(
            RunEvaluation,
            (RunEvaluation.test_id == GoldAnnotation.test_id)
            & (RunEvaluation.run_id == GoldAnnotation.run_id)
            & (RunEvaluation.judge_model_id == judge_model_id),
        )
    )
    rows = list(session.execute(stmt).all())
    if not rows:
        return dict(
            n_pairs=0,
            response_kappa=None, response_spearman=None,
            citation_kappa=None, citation_spearman=None,
        )
    gold_resp_labels = [r[0] for r in rows]
    judge_resp_labels = [r[1] or "" for r in rows]
    gold_resp_scores = [float(r[2]) for r in rows]
    judge_resp_scores = [float(r[3]) if r[3] is not None else 0.0 for r in rows]
    gold_cit_labels = [r[4] for r in rows]
    judge_cit_labels = [r[5] or "" for r in rows]
    gold_cit_scores = [float(r[6]) for r in rows]
    judge_cit_scores = [float(r[7]) if r[7] is not None else 0.0 for r in rows]

    return dict(
        n_pairs=len(rows),
        response_kappa=cohen_kappa(gold_resp_labels, judge_resp_labels),
        response_spearman=spearman_rho(gold_resp_scores, judge_resp_scores),
        citation_kappa=cohen_kappa(gold_cit_labels, judge_cit_labels),
        citation_spearman=spearman_rho(gold_cit_scores, judge_cit_scores),
    )
