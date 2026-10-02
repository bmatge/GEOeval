"""Routers de l'API v1, par ressource."""
from __future__ import annotations

from geoeval.web.api.v1 import jobs, models, orgs, perimeters, questions, runs, schedules, stats, tokens

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
]
