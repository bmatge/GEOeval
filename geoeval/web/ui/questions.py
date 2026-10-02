"""Questions (tests) et vérité de référence (ADR-079 §1).

Router UI (lot 1.3a) : découpage mécanique de l'ancien app.py, sans changement
de comportement. Les règles métier vivent dans les services (geoeval.web.*).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from geoeval.web import (
    audit,
    ground_truth,
    perimeters,
    services,
)
from geoeval.web.auth import CurrentUser
from geoeval.web.deps import (
    get_db,
    require_org,
    require_role,
    require_user,
)
from geoeval.web.rendering import opt_int, render

router = APIRouter()


@router.get("/o/{org_slug}/tests", response_class=HTMLResponse)
def tests(
    request: Request, ctx=Depends(require_org), db: Session = Depends(get_db),
    perimeter: str = "",
):
    org, role = ctx
    # Filtre optionnel par slug de périmètre.
    peri_id: Optional[int] = None
    peri_obj = None
    if perimeter:
        peri_obj = perimeters.get_by_slug(db, org.id, perimeter)
        peri_id = peri_obj.id if peri_obj else -1
    return render(
        request, "tests.html", active="tests", org=org, role=role,
        tests=services.list_tests(db, org.id, perimeter_id=peri_id),
        all_perimeters=perimeters.list_for_org(db, org.id),
        current_perimeter=peri_obj,
    )


@router.get("/o/{org_slug}/tests/new", response_class=HTMLResponse)
def test_new(
    request: Request,
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
    perimeter_id: int = 0,
):
    org, role = ctx
    default_peri = None
    if perimeter_id > 0:
        default_peri = perimeters.get_by_id(db, org.id, perimeter_id)
    return render(
        request, "test_form.html", active="tests", org=org, role=role,
        test=None,
        prompts=services.list_prompts(db),
        all_perimeters=perimeters.list_for_org(db, org.id),
        default_perimeter=default_peri,
    )


@router.post("/o/{org_slug}/tests/new")
def test_create(
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
    perimeter_id: int = Form(...),
    prompt: str = Form(...),
    expected_answer: str = Form(""),
    response_quality_prompt_id: str = Form(""),
    citation_quality_prompt_id: str = Form(""),
):
    org, _ = ctx
    peri = perimeters.get_by_id(db, org.id, perimeter_id)
    if peri is None:
        raise HTTPException(status_code=400, detail="Périmètre invalide.")
    services.create_test(
        db, org.id, perimeter_id=perimeter_id,
        prompt=prompt, expected_answer=expected_answer,
        response_quality_prompt_id=opt_int(response_quality_prompt_id),
        citation_quality_prompt_id=opt_int(citation_quality_prompt_id),
    )
    return RedirectResponse(f"/o/{org.slug}/perimeters/{perimeter_id}", status_code=303)


@router.get("/o/{org_slug}/tests/{test_id}/edit", response_class=HTMLResponse)
def test_edit(test_id: int, request: Request, ctx=Depends(require_role("editor")), db: Session = Depends(get_db)):
    org, role = ctx
    test = services.get_test(db, org.id, test_id)
    if test is None:
        raise HTTPException(status_code=404, detail=f"Test {test_id} introuvable")
    return render(
        request, "test_form.html", active="tests", org=org, role=role,
        test=test,
        prompts=services.list_prompts(db),
        all_perimeters=perimeters.list_for_org(db, org.id),
        default_perimeter=None,
    )


@router.post("/o/{org_slug}/tests/{test_id}/edit")
def test_update(
    test_id: int,
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
    perimeter_id: int = Form(...),
    prompt: str = Form(...),
    expected_answer: str = Form(""),
    response_quality_prompt_id: str = Form(""),
    citation_quality_prompt_id: str = Form(""),
):
    org, _ = ctx
    # Déplacement éventuel de périmètre.
    peri = perimeters.get_by_id(db, org.id, perimeter_id)
    if peri is None:
        raise HTTPException(status_code=400, detail="Périmètre invalide.")
    test = services.get_test(db, org.id, test_id)
    if test is None:
        raise HTTPException(status_code=404, detail="Test introuvable.")
    if test.perimeter_id != perimeter_id:
        test.perimeter_id = perimeter_id
        db.commit()
    services.update_test(
        db, org.id, test_id,
        prompt=prompt, expected_answer=expected_answer,
        response_quality_prompt_id=opt_int(response_quality_prompt_id),
        citation_quality_prompt_id=opt_int(citation_quality_prompt_id),
    )
    return RedirectResponse(f"/o/{org.slug}/perimeters/{perimeter_id}", status_code=303)


@router.post("/o/{org_slug}/tests/{test_id}/deactivate")
def test_deactivate(test_id: int, ctx=Depends(require_role("editor")), db: Session = Depends(get_db)):
    org, _ = ctx
    services.deactivate_test(db, org.id, test_id)
    return RedirectResponse(f"/o/{org.slug}/tests", status_code=303)


@router.post("/o/{org_slug}/tests/{test_id}/reactivate")
def test_reactivate(test_id: int, ctx=Depends(require_role("editor")), db: Session = Depends(get_db)):
    org, _ = ctx
    services.reactivate_test(db, org.id, test_id)
    return RedirectResponse(f"/o/{org.slug}/tests", status_code=303)


# ---- Vérité de référence (ADR-079 §1) --------------------------------
@router.get("/o/{org_slug}/tests/{test_id}", response_class=HTMLResponse)
def test_detail(
    test_id: int,
    request: Request,
    ctx=Depends(require_org),
    db: Session = Depends(get_db),
):
    org, role = ctx
    test = services.get_test(db, org.id, test_id)
    if test is None:
        raise HTTPException(status_code=404, detail=f"Test {test_id} introuvable")
    versions = ground_truth.list_versions(db, test_id)
    return render(
        request, "test_detail.html", active="tests", org=org, role=role,
        test=test, gt_versions=versions,
    )


@router.post("/o/{org_slug}/tests/{test_id}/ground-truth")
def test_ground_truth_create(
    test_id: int,
    ctx=Depends(require_role("editor")),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
    reference_answer: str = Form(...),
    reference_urls: str = Form(""),
    notes: str = Form(""),
):
    org, _ = ctx
    test = services.get_test(db, org.id, test_id)
    if test is None:
        raise HTTPException(status_code=404, detail="Test introuvable")
    urls = [u.strip() for u in reference_urls.replace("\n", ",").split(",") if u.strip()]
    row = ground_truth.create_new_version(
        db,
        test_id=test_id,
        reference_answer=reference_answer,
        reference_urls=urls,
        created_by=user.id,
        notes=notes or None,
    )
    audit.record(
        db, user_id=user.id, org_id=org.id, action="create",
        entity_type="test_ground_truth", entity_id=row.id,
        meta={"test_id": test_id, "version": row.version},
    )
    return RedirectResponse(f"/o/{org.slug}/tests/{test_id}", status_code=303)
