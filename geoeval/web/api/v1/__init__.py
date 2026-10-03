"""Routers de l'API v1, par ressource."""
from __future__ import annotations

from geoeval.web.api.v1 import (
    budget, contracts, jobs, models, orgs, perimeters, pools, questions, runs, schedules, stats, themes, tokens,
)

ROUTERS = [
    orgs.router,
    perimeters.router,
    questions.router,
    models.router,
    runs.router,
    stats.router,
    schedules.router,
    jobs.router,
    tokens.router,
    budget.router,
    pools.router,
    themes.router,
    contracts.router,
]
