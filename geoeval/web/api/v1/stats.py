"""Statistiques agrégées — lecture publique (ADR-087). Mêmes données que le tableau de bord."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from geoeval.web import services
from geoeval.web.api.deps import Principal, org_context
from geoeval.web.api.schemas import (
    EvolutionPointOut,
    LeaderboardRowOut,
    QuestionStatOut,
    ScoreRowOut,
    StatsSummaryOut,
)
from geoeval.web.deps import get_db

router = APIRouter(prefix="/orgs/{org_slug}/stats", tags=["statistiques"])


@router.get("/summary", response_model=StatsSummaryOut, summary="Agrégats globaux")
def summary(principal: Principal = Depends(org_context), db: Session = Depends(get_db)):
    return services.org_stats_summary(db, principal.org.id)


@router.get("/leaderboard", response_model=list[LeaderboardRowOut], summary="Classement des IA évaluées")
def leaderboard(principal: Principal = Depends(org_context), db: Session = Depends(get_db)):
    return services.leaderboard(db, principal.org.id)


@router.get("/questions", response_model=list[QuestionStatOut], summary="Scores moyens par question, pires d'abord")
def questions(principal: Principal = Depends(org_context), db: Session = Depends(get_db)):
    return services.question_stats(db, principal.org.id)


@router.get("/evolution", response_model=list[EvolutionPointOut], summary="Scores par IA et par passage")
def evolution(principal: Principal = Depends(org_context), db: Session = Depends(get_db)):
    return services.model_evolution(db, principal.org.id)


@router.get("/scores", response_model=list[ScoreRowOut],
            summary="Notes par évaluation et par question, pour l'entité et son sous-arbre (explorateur de scores)")
def scores(principal: Principal = Depends(org_context), db: Session = Depends(get_db), history: bool = False):
    """Par défaut, la dernière évaluation de chaque (entité, périmètre, IA). `history=true` : tous les
    passages, plus récents d'abord (5 000 lignes au plus)."""
    return services.score_rows(db, principal.org, history=history)
