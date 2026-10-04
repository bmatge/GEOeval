"""Rejugement versionné et calibration des notateurs (suite E8, ADR-089 §2.9).

Lecture : membres de l'entité (viewer+). Lancer un rejugement, annoter : editor+.
Les refus de lancement (routage, contrats, budget…) sortent en problem+json typés.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from geoeval.web import audit, calibration, detectors, rejudge
from geoeval.web.api.deps import Principal, require_role
from geoeval.web.api.schemas import (
    AgreementMetricsOut,
    AnnotationIn,
    AnnotationOut,
    CalibrationOut,
    ComparisonLineOut,
    ComparisonPairOut,
    EvaluationBatchIn,
    EvaluationBatchOut,
    JudgeAgreementOut,
    PromotionOut,
)
from geoeval.web.deps import get_db

router = APIRouter(prefix="/orgs/{org_slug}", tags=["rejugement et calibration"])


def _out(db: Session, b, *, with_comparison: bool) -> EvaluationBatchOut:
    out = EvaluationBatchOut(
        id=b.id, label=b.label, status=rejudge.status(db, b), run_ids=list(b.run_ids), judges=list(b.judges),
        response_prompt_id=b.response_prompt_id, citation_prompt_id=b.citation_prompt_id, grids=dict(b.grids or {}),
        estimate_eur=b.estimate_eur, job_id=b.job_id, created_at=b.created_at, promoted_at=b.promoted_at,
        promoted_run_ids=rejudge.promoted_runs(db, b),
    )
    if with_comparison:
        cmp = rejudge.compare(db, b)
        out.summary = ComparisonLineOut(**cmp.summary)
        out.by_model = [ComparisonLineOut(**m) for m in cmp.by_model]
        out.pairs = [ComparisonPairOut(**{k: v for k, v in p.items() if k != "prompt"}) for p in cmp.pairs]
    return out


@router.get("/rejudge", response_model=list[EvaluationBatchOut], summary="Lots de rejugement de l'entité")
def list_batches(principal: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    return [_out(db, b, with_comparison=False) for b in rejudge.list_for_org(db, principal.org)]


@router.post("/rejudge", response_model=EvaluationBatchOut, status_code=status.HTTP_201_CREATED,
             summary="Rejuger des runs de l'entité (editor+) : contrôles et devis, puis mise en file")
def create_batch(body: EvaluationBatchIn, principal: Principal = Depends(require_role("editor")),
                 db: Session = Depends(get_db)):
    b = rejudge.create(
        db, principal.org, run_ids=body.run_ids, judge_models=body.judge_models, repeats=body.repeats,
        response_prompt_id=body.response_prompt_id, citation_prompt_id=body.citation_prompt_id, label=body.label,
        created_by=principal.user_id, role=principal.role, is_platform_admin=principal.is_platform_admin,
    )
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action="create", entity_type="evaluation_batch",
                 entity_id=b.id, meta={"runs": b.run_ids, "judges": [j["model_version"] for j in b.judges],
                                       "estimate_eur": str(b.estimate_eur), "job_id": b.job_id, **principal.audit_meta()})
    return _out(db, b, with_comparison=False)


@router.get("/rejudge/{batch_id}", response_model=EvaluationBatchOut,
            summary="Lot de rejugement et comparaison avec les notes d'origine")
def get_batch(batch_id: int, principal: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    return _out(db, rejudge.get_for_org(db, principal.org, batch_id), with_comparison=True)


@router.post("/rejudge/{batch_id}/promote", response_model=PromotionOut,
             summary="Promouvoir le lot : ses notes deviennent officielles (org_admin, hors runs de campagne)")
def promote_batch(batch_id: int, principal: Principal = Depends(require_role("org_admin")), db: Session = Depends(get_db)):
    b = rejudge.get_for_org(db, principal.org, batch_id)
    res = rejudge.promote(db, principal.org, b, user_id=principal.user_id)
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action="promote", entity_type="evaluation_batch",
                 entity_id=b.id, meta={"runs": res.done, "skipped": [r for r, _ in res.skipped], **principal.audit_meta()})
    return PromotionOut(promoted_run_ids=res.done, skipped=[{"run_id": r, "reason": why} for r, why in res.skipped],
                        batch=_out(db, b, with_comparison=False))


@router.post("/rejudge/{batch_id}/revert", response_model=EvaluationBatchOut,
             summary="Restaurer les notes d'origine des runs dont ce lot est la référence (org_admin)")
def revert_batch(batch_id: int, principal: Principal = Depends(require_role("org_admin")), db: Session = Depends(get_db)):
    b = rejudge.get_for_org(db, principal.org, batch_id)
    restored = rejudge.revert(db, principal.org, b)
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action="revert", entity_type="evaluation_batch",
                 entity_id=b.id, meta={"runs": restored, **principal.audit_meta()})
    return _out(db, b, with_comparison=False)


@router.get("/calibration", response_model=CalibrationOut,
            summary="Accord des notateurs avec le jeu de calibration de l'entité (global et par thème)")
def get_calibration(principal: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    org = principal.org
    eff = detectors.effective_settings(db, org)
    _, min_pairs = detectors.calibration_defaults()

    def below(m: dict) -> bool:
        rho = m.get("response_spearman")
        return m.get("n_pairs", 0) >= min_pairs and rho is not None and rho < float(eff.calibration_min_rho)

    judges = [JudgeAgreementOut(
        model_version=j.model.model_version, batch_id=j.batch.id if j.batch else None,
        overall=AgreementMetricsOut(**j.overall), by_theme=[AgreementMetricsOut(**t) for t in j.by_theme],
        below_threshold=below(j.overall) or any(below(t) for t in j.by_theme),
    ) for j in calibration.overview(db, org)]
    return CalibrationOut(n_gold_pairs=len(calibration.gold_pairs(db, org)), threshold=eff.calibration_min_rho,
                          threshold_from_org_slug=eff.calibration_from.slug if eff.calibration_from else None,
                          min_pairs=min_pairs, judges=judges)


@router.get("/annotations", response_model=list[AnnotationOut], summary="Annotations propres à l'entité")
def list_annotations(principal: Principal = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    return [AnnotationOut.model_validate(a) for a in calibration.list_annotations(db, principal.org, own_only=True)]


@router.put("/annotations", response_model=AnnotationOut,
            summary="Annoter un résultat d'un run de l'entité (editor+, session : l'annotateur est l'utilisateur)")
def put_annotation(body: AnnotationIn, principal: Principal = Depends(require_role("editor")),
                   db: Session = Depends(get_db)):
    if principal.kind != "session" or not principal.email:
        raise HTTPException(status_code=403, detail="Annotation personnelle : session requise (jeton d'organisation refusé).")
    try:
        a = calibration.annotate(
            db, principal.org, run_id=body.run_id, test_id=body.test_id, user_id=principal.user_id,
            email=principal.email, response_label=body.response_label, response_score=body.response_score,
            citation_label=body.citation_label, citation_score=body.citation_score, notes=body.notes or "",
        )
    except calibration.CalibrationError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    audit.record(db, user_id=principal.user_id, org_id=principal.org.id, action="annotate", entity_type="gold_annotation",
                 entity_id=a.id, meta={"run_id": a.run_id, "test_id": a.test_id, **principal.audit_meta()})
    return AnnotationOut.model_validate(a)
