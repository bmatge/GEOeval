"""Contrats LLM et politique de routage d'une entité (E5, ADR-089 §2.6).

Contrats : org_admin (rôle hérité compris) ; la clé est en écriture seule.
Politique : lecture editor+, écriture org_admin.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from geoeval.web import audit, contracts, routing, services
from geoeval.web.api.deps import Principal, require_role
from geoeval.web.api.schemas import (
    ContractIn,
    ContractOut,
    ContractPatch,
    KeyResolutionOut,
    RoutingPolicyIn,
    RoutingPolicyOut,
)
from geoeval.web.deps import get_db

router = APIRouter(prefix="/orgs/{org_slug}", tags=["contrats LLM"])


def _http(exc: contracts.ContractError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


def _out(c, owner, viewer, used) -> ContractOut:
    return ContractOut(
        id=c.id, owner_org_slug=owner.slug, inherited=owner.id != viewer.id, family=c.family, label=c.label,
        reference=c.reference, base_url=c.base_url, has_api_key=bool(c.api_key_encrypted),
        header_names=sorted((c.extra_headers or {}).keys()), model_ids=list(c.model_ids or []),
        valid_from=c.valid_from, valid_to=c.valid_to, cap_eur=c.cap_eur, spent_eur=used,
        hosting=c.hosting, sovereign=c.sovereign, is_active=c.is_active, status=contracts.status(c, used),
    )


def _audit(db: Session, principal: Principal, action: str, entity_id: int, **meta) -> None:
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action=action, entity_type="llm_contract",
                 entity_id=entity_id, meta={**meta, **principal.audit_meta()})


@router.get("/contracts", response_model=list[ContractOut], summary="Contrats propres et hérités (org_admin)")
def list_contracts(principal: Principal = Depends(require_role("org_admin")), db: Session = Depends(get_db)):
    org = principal.org
    rows = [(c, org) for c in contracts.list_own(db, org.id)] + contracts.list_inherited(db, org)
    used = contracts.spent_by_contract(db, [c.id for c, _ in rows])
    return [_out(c, o, org, used.get(c.id, 0)) for c, o in rows]


@router.get("/contracts/resolution", response_model=list[KeyResolutionOut],
            summary="Clé utilisée pour chaque IA active : contrat, plateforme ou blocage (org_admin)")
def key_resolution(principal: Principal = Depends(require_role("org_admin")), db: Session = Depends(get_db)):
    out = []
    for m in services.list_models(db):
        res = contracts.resolve(db, principal.org, m)
        source = "blocked" if not res.usable else ("contract" if res.contract else "platform")
        out.append(KeyResolutionOut(
            model_id=m.model_id, model_version=m.model_version, source=source,
            contract_id=res.contract.id if res.contract else None,
            owner_org_slug=res.owner.slug if res.owner else None, blocked_reason=res.blocked,
        ))
    return out


@router.post("/contracts", response_model=ContractOut, status_code=status.HTTP_201_CREATED,
             summary="Créer un contrat (org_admin)")
def create_contract(body: ContractIn, principal: Principal = Depends(require_role("org_admin")), db: Session = Depends(get_db)):
    data = body.model_dump()
    try:
        c = contracts.create(db, principal.org, created_by=principal.user_id, **data)
    except contracts.ContractError as exc:
        raise _http(exc)
    _audit(db, principal, "create", c.id, family=c.family, label=c.label)
    return _out(c, principal.org, principal.org, contracts.spent(db, c.id))


def _owned(db: Session, principal: Principal, contract_id: int):
    try:
        return contracts.get_owned(db, principal.org, contract_id)
    except contracts.ContractError as exc:
        raise _http(exc)


@router.get("/contracts/{contract_id}", response_model=ContractOut, summary="Un contrat propre (org_admin)")
def get_contract(contract_id: int, principal: Principal = Depends(require_role("org_admin")), db: Session = Depends(get_db)):
    c = _owned(db, principal, contract_id)
    return _out(c, principal.org, principal.org, contracts.spent(db, c.id))


@router.patch("/contracts/{contract_id}", response_model=ContractOut,
              summary="Modifier un contrat (org_admin) — la famille ne change pas")
def update_contract(contract_id: int, body: ContractPatch, principal: Principal = Depends(require_role("org_admin")),
                    db: Session = Depends(get_db)):
    c = _owned(db, principal, contract_id)
    fields = body.model_dump(exclude_unset=True)
    api_key = fields.pop("api_key", None)
    clear = fields.pop("clear_api_key", False)
    try:
        contracts.update(db, c, api_key=api_key, clear_api_key=clear, **fields)
    except contracts.ContractError as exc:
        raise _http(exc)
    _audit(db, principal, "update", c.id, fields=sorted(fields), key_changed=bool(api_key) or clear)
    return _out(c, principal.org, principal.org, contracts.spent(db, c.id))


@router.delete("/contracts/{contract_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Supprimer un contrat jamais utilisé (org_admin) — 409 sinon : le désactiver")
def delete_contract(contract_id: int, principal: Principal = Depends(require_role("org_admin")), db: Session = Depends(get_db)):
    c = _owned(db, principal, contract_id)
    label = c.label
    try:
        contracts.delete(db, c)
    except contracts.ContractError as exc:
        raise _http(exc)
    _audit(db, principal, "delete", contract_id, label=label)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _policy_out(db: Session, org) -> RoutingPolicyOut:
    own = routing.get_own(db, org.id)
    eff = routing.effective(db, org)
    return RoutingPolicyOut(
        own=RoutingPolicyIn(
            allowed_families=own.allowed_families if own else None,
            sovereign_only=own.sovereign_only if own else False, eu_only=own.eu_only if own else False,
        ),
        effective_allowed_families=sorted(eff.allowed_families) if eff.allowed_families is not None else None,
        effective_sovereign_only=eff.sovereign_only, effective_eu_only=eff.eu_only,
    )


@router.get("/routing-policy", response_model=RoutingPolicyOut, summary="Politique de routage propre et effective (editor+)")
def get_policy(principal: Principal = Depends(require_role("editor")), db: Session = Depends(get_db)):
    return _policy_out(db, principal.org)


@router.put("/routing-policy", response_model=RoutingPolicyOut,
            summary="Remplacer la politique propre de l'entité (org_admin) — restrictive, s'ajoute à l'héritée")
def put_policy(body: RoutingPolicyIn, principal: Principal = Depends(require_role("org_admin")), db: Session = Depends(get_db)):
    try:
        routing.set_policy(db, principal.org, allowed_families=body.allowed_families,
                           sovereign_only=body.sovereign_only, eu_only=body.eu_only, updated_by=principal.user_id)
    except contracts.ContractError as exc:
        raise _http(exc)
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action="update", entity_type="routing_policy",
                 entity_id=principal.org.id, meta={**body.model_dump(), **principal.audit_meta()})
    return _policy_out(db, principal.org)
