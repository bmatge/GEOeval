"""
Routers UI (HTML DSFR) — lot 1.3a.

L'ordre d'inclusion compte pour FastAPI : les chemins fixes (`/tests/new`)
doivent précéder les chemins paramétrés du même préfixe (`/tests/{test_id}`),
ce que chaque module respecte en interne ; entre modules, aucun chevauchement.
"""
from __future__ import annotations

from geoeval.web.ui import (
    admin,
    contracts,
    dashboard,
    home,
    jobs,
    launch,
    methodology,
    models,
    notifications,
    perimeters,
    pools,
    prompts,
    questions,
    runs,
    schedules,
    settings,
)

ROUTERS = [
    home.router,
    dashboard.router,
    runs.router,
    perimeters.router,
    questions.router,
    pools.router,
    prompts.router,
    launch.router,
    models.router,
    schedules.router,
    jobs.router,
    settings.router,
    contracts.router,
    notifications.router,
    admin.router,
    methodology.router,
]
