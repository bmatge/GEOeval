"""
Erreurs de l'API v1 au format RFC 9457 « Problem Details » (arbitrage lot 1.3).

Corps : {type, title, status, detail, instance, …extensions}, Content-Type
``application/problem+json``. Les refus de lancement (LaunchError) portent un
`type` dédié et leurs extensions (modèles interdits, estimation).
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from geoeval.web.launching import LaunchError

PROBLEM_MEDIA_TYPE = "application/problem+json"

TITLES = {
    400: "Requête invalide",
    401: "Authentification requise",
    402: "Plafond budgétaire atteint",
    403: "Accès refusé",
    404: "Ressource introuvable",
    405: "Méthode non autorisée",
    409: "Conflit",
    422: "Requête invalide",
    500: "Erreur interne",
}

LAUNCH_TITLES = {
    "validation": "Sélection invalide",
    "forbidden_models": "Modèles non autorisés pour cette organisation",
    "budget": "Plafond budgétaire atteint",
    "not_found": "Ressource introuvable",
}


def problem(
    status: int,
    *,
    title: Optional[str] = None,
    detail: Optional[str] = None,
    type_: str = "about:blank",
    instance: Optional[str] = None,
    headers: Optional[dict[str, str]] = None,
    **extensions: Any,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": type_,
        "title": title or TITLES.get(status, "Erreur"),
        "status": status,
    }
    if detail:
        body["detail"] = detail
    if instance:
        body["instance"] = instance
    body.update({k: v for k, v in extensions.items() if v is not None})
    return JSONResponse(status_code=status, content=jsonable_encoder(body), media_type=PROBLEM_MEDIA_TYPE, headers=headers)


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException):
        headers = dict(exc.headers or {})
        if exc.status_code == 401:
            headers.setdefault("WWW-Authenticate", "Bearer")
        return problem(exc.status_code, detail=str(exc.detail) if exc.detail else None,
                       instance=request.url.path, headers=headers or None)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        return problem(422, detail="Le corps ou les paramètres de la requête sont invalides.",
                       instance=request.url.path, errors=exc.errors())

    @app.exception_handler(LaunchError)
    async def _launch(request: Request, exc: LaunchError):
        return problem(
            exc.status,
            title=LAUNCH_TITLES.get(exc.kind),
            detail=exc.detail,
            type_=f"/api/v1/problems/{exc.kind.replace('_', '-')}",
            instance=request.url.path,
            **exc.extra,
        )
