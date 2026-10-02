"""Jetons d'API de l'organisation — org_admin. Le clair n'est renvoyé qu'à la création."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from geoeval.web import api_tokens, audit
from geoeval.web.api.deps import Principal, require_role
from geoeval.web.api.schemas import TokenCreatedOut, TokenCreateIn, TokenOut
from geoeval.web.deps import get_db

router = APIRouter(prefix="/orgs/{org_slug}/tokens", tags=["jetons"])


def _out(t) -> TokenOut:
    out = TokenOut.model_validate(t)
    out.active = api_tokens.is_valid(t)
    return out


@router.get("", response_model=list[TokenOut], summary="Jetons de l'organisation (org_admin)")
def list_tokens(principal: Principal = Depends(require_role("org_admin")), db: Session = Depends(get_db)):
    return [_out(t) for t in api_tokens.list_for_org(db, principal.org.id)]


@router.post("", response_model=TokenCreatedOut, status_code=status.HTTP_201_CREATED,
             summary="Créer un jeton (org_admin) — le clair n'est montré qu'une fois")
def create_token(body: TokenCreateIn, principal: Principal = Depends(require_role("org_admin")), db: Session = Depends(get_db)):
    try:
        token, plaintext = api_tokens.create(
            db, org_id=principal.org.id, name=body.name, role=body.role,
            created_by=principal.user_id, expires_in_days=body.expires_in_days,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action="create", entity_type="api_token",
                 entity_id=token.id, meta={"name": token.name, "role": token.role, "prefix": token.prefix, **principal.audit_meta()})
    base = TokenOut.model_validate(token).model_dump()
    base["active"] = True
    return TokenCreatedOut(**base, token=plaintext)


@router.delete("/{token_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Révoquer un jeton (org_admin)")
def revoke_token(token_id: int, principal: Principal = Depends(require_role("org_admin")), db: Session = Depends(get_db)):
    try:
        token = api_tokens.revoke(db, principal.org.id, token_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Jeton introuvable.")
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action="revoke", entity_type="api_token",
                 entity_id=token.id, meta={"prefix": token.prefix, **principal.audit_meta()})
    return Response(status_code=status.HTTP_204_NO_CONTENT)
