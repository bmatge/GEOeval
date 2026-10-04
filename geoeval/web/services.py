"""
Couche d'accès aux données pour l'UI web.

Retourne des dicts prêts pour les templates (scores convertis en float ou None)
afin de garder les templates Jinja simples.

Isolation par organisation (ADR-077) : les DAO manipulant des données
métier prennent `org_id` en premier argument obligatoire. Les entités
globales — catalogue de modèles, prompts d'évaluation, types — n'ont pas de
`organization_id` (partagées entre orgs).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from geoeval.db.models import (
    EvaluationPrompt,
    Model,
    Organization,
    Perimeter,
    PromptType,
    RunEvaluation,
    RunResult,
    RunRow,
    ScheduledRun,
    Test,
)
from geoeval.web import perimeters as perimeters_svc


def _f(x: Any) -> Optional[float]:
    """Decimal|None -> float|None (pour l'affichage)."""
    if x is None:
        return None
    if isinstance(x, Decimal):
        return float(x)
    return float(x)


# -----------------------------
# Tableau de bord / runs
# -----------------------------
def leaderboard(session: Session, org_id: int) -> list[dict[str, Any]]:
    stmt = (
        select(
            Model.model_id,
            Model.model_name,
            Model.model_version,
            func.count(func.distinct(RunRow.run_id)).label("n_runs"),
            func.count(RunEvaluation.test_id).label("n_evals"),
            func.avg(RunEvaluation.response_quality_score).label("avg_response"),
            func.avg(RunEvaluation.citation_quality_score).label("avg_citation"),
        )
        .join(RunRow, RunRow.tested_model_id == Model.model_id)
        .join(RunEvaluation, RunEvaluation.run_id == RunRow.run_id)
        .where(RunRow.organization_id == org_id)
        .group_by(Model.model_id, Model.model_name, Model.model_version)
        .order_by(func.avg(RunEvaluation.response_quality_score).desc().nullslast())
    )
    out = []
    for r in session.execute(stmt).all():
        out.append(
            dict(
                model_id=r.model_id,
                model_name=r.model_name,
                model_version=r.model_version,
                n_runs=r.n_runs,
                n_evals=r.n_evals,
                avg_response=_f(r.avg_response),
                avg_citation=_f(r.avg_citation),
            )
        )
    return out


def list_runs(session: Session, org_id: int) -> list[dict[str, Any]]:
    stmt = (
        select(
            RunRow.run_id,
            RunRow.started_at,
            RunRow.run_meta,
            Model.model_name,
            Model.model_version,
            func.count(RunEvaluation.test_id).label("n_evals"),
            func.avg(RunEvaluation.response_quality_score).label("avg_response"),
            func.avg(RunEvaluation.citation_quality_score).label("avg_citation"),
        )
        .join(Model, Model.model_id == RunRow.tested_model_id)
        .outerjoin(RunEvaluation, RunEvaluation.run_id == RunRow.run_id)
        .where(RunRow.organization_id == org_id)
        .group_by(
            RunRow.run_id,
            RunRow.started_at,
            RunRow.run_meta,
            Model.model_name,
            Model.model_version,
        )
        .order_by(RunRow.run_id.desc())
    )
    out = []
    for r in session.execute(stmt).all():
        out.append(
            dict(
                run_id=r.run_id,
                started_at=r.started_at,
                run_meta=r.run_meta,
                model_name=r.model_name,
                model_version=r.model_version,
                n_evals=r.n_evals,
                avg_response=_f(r.avg_response),
                avg_citation=_f(r.avg_citation),
            )
        )
    return out


def get_run_detail(session: Session, org_id: int, run_id: int) -> Optional[dict[str, Any]]:
    run = session.execute(
        select(RunRow, Model)
        .join(Model, Model.model_id == RunRow.tested_model_id)
        .where(RunRow.run_id == run_id, RunRow.organization_id == org_id)
    ).first()
    if run is None:
        return None
    run_row, tested_model = run

    result_rows = session.execute(
        select(RunResult, Test)
        .join(Test, Test.test_id == RunResult.test_id)
        .where(RunResult.run_id == run_id)
        .order_by(RunResult.test_id)
    ).all()

    eval_rows = session.execute(
        select(RunEvaluation, Model.model_version)
        .join(Model, Model.model_id == RunEvaluation.judge_model_id)
        .where(RunEvaluation.run_id == run_id)
        .order_by(RunEvaluation.test_id, RunEvaluation.judge_model_id, RunEvaluation.judge_run_index)
    ).all()

    evals_by_test: dict[int, list[dict[str, Any]]] = {}
    for ev, judge_version in eval_rows:
        evals_by_test.setdefault(ev.test_id, []).append(
            dict(
                judge_version=judge_version,
                judge_run_index=ev.judge_run_index,
                response_score=_f(ev.response_quality_score),
                response_label=ev.response_quality_label,
                citation_score=_f(ev.citation_quality_score),
                citation_label=ev.citation_quality_label,
            )
        )

    # E4 : domaines officiels du périmètre (valeur courante) → part des citations officielles.
    peri = session.get(Perimeter, run_row.perimeter_id) if run_row.perimeter_id else None
    domains = list(peri.domains or []) if peri is not None else []
    all_urls: list[str] = []
    results = []
    for rr, test in result_rows:
        citations = rr.raw_citations or []
        urls = [_citation_url(c) for c in citations]
        all_urls.extend(u for u in urls if u)
        results.append(
            dict(
                test_id=test.test_id,
                prompt=test.prompt,
                expected_answer=test.expected_answer,
                raw_answer=rr.raw_answer,
                raw_citations=citations,
                official_citations={u for u in urls if u and perimeters_svc.is_official(u, domains)},
                official_share=perimeters_svc.official_share(urls, domains),
                evals=evals_by_test.get(test.test_id, []),
            )
        )

    return dict(
        run_id=run_row.run_id,
        started_at=run_row.started_at,
        run_meta=run_row.run_meta,
        model_name=tested_model.model_name,
        model_version=tested_model.model_version,
        results=results,
        official_domains=domains,
        official_share=perimeters_svc.official_share(all_urls, domains),
    )


def _citation_url(c: Any) -> str:
    """Une citation brute est une URL, ou un objet portant une clé `url`."""
    if isinstance(c, dict):
        return str(c.get("url") or "")
    return str(c or "")


def question_stats(session: Session, org_id: int) -> list[dict[str, Any]]:
    """Scores moyens par question (toutes évaluations confondues), pires d'abord.

    Alimente le graphique « quelles questions sont bien/mal notées » du
    tableau de bord (dsfr-data, barres horizontales).
    """
    stmt = (
        select(
            Test.test_id,
            Test.prompt,
            func.count(RunEvaluation.judge_model_id).label("n_evals"),
            func.avg(RunEvaluation.response_quality_score).label("avg_response"),
            func.avg(RunEvaluation.citation_quality_score).label("avg_citation"),
        )
        .join(RunEvaluation, RunEvaluation.test_id == Test.test_id)
        .join(RunRow, RunRow.run_id == RunEvaluation.run_id)
        # E4 : une question de pool partagé appartient à une autre org, mais ses
        # évaluations dans NOS runs comptent : on filtre sur le run, pas la question.
        .where(RunRow.organization_id == org_id)
        .group_by(Test.test_id, Test.prompt)
        .order_by(func.avg(RunEvaluation.response_quality_score).asc().nullslast())
    )
    out = []
    for r in session.execute(stmt).all():
        excerpt = r.prompt if len(r.prompt) <= 60 else r.prompt[:57] + "…"
        out.append(
            dict(
                test_id=r.test_id,
                label=f"Q{r.test_id} · {excerpt}",
                prompt=r.prompt,
                n_evals=int(r.n_evals),
                avg_response=_f(r.avg_response),
                avg_citation=_f(r.avg_citation),
            )
        )
    return out


def model_evolution(session: Session, org_id: int) -> list[dict[str, Any]]:
    """Scores par IA en fonction du n° de passage (format long pour series-field).

    L'abscisse est le rang chronologique du run POUR CHAQUE modèle (« Éval 1 »,
    « Éval 2 », …) et non le run_id global : chaque IA a ainsi une valeur à
    chaque position — courbes continues et comparables (un run n'appartient
    qu'à un modèle ; sur un axe en run_id les autres séries tomberaient à 0).
    """
    rows = list_runs(session, org_id)  # desc par run_id
    rows = [r for r in reversed(rows) if r["n_evals"]]  # chronologique, évalués
    seq: dict[str, int] = {}
    out = []
    for r in rows:
        model = r["model_version"]
        seq[model] = seq.get(model, 0) + 1
        out.append(
            dict(
                label=f"Éval {seq[model]}",
                seq=seq[model],
                model=model,
                run_id=r["run_id"],
                avg_response=r["avg_response"],
                avg_citation=r["avg_citation"],
            )
        )
    return out


SCORE_ROWS_LIMIT = 5000


def score_rows(session: Session, org: Organization, *, history: bool = False,
               limit: int = SCORE_ROWS_LIMIT) -> list[dict[str, Any]]:
    """Une ligne par (évaluation, question) pour l'entité **et son sous-arbre** : entité,
    périmètre, IA évaluée, question, notes moyennes des notateurs (réponse, citations).
    Format plat pour l'explorateur de scores (facettes, recherche, tableau, matrice).

    Par défaut, seule la dernière évaluation de chaque (entité, périmètre, IA) est gardée :
    c'est l'état courant. `history=True` rend tous les passages, plus récents d'abord, bornés à `limit`."""
    from geoeval.web import hierarchy

    org_ids = hierarchy.descendant_ids(session, org, include_self=True)
    runs = select(RunRow).where(RunRow.organization_id.in_(org_ids))
    if not history:
        latest = (
            select(func.max(RunRow.run_id))
            .where(RunRow.organization_id.in_(org_ids))
            .group_by(RunRow.organization_id, RunRow.perimeter_id, RunRow.tested_model_id)
        )
        runs = runs.where(RunRow.run_id.in_(latest))
    runs = runs.subquery()
    stmt = (
        select(
            runs.c.run_id, runs.c.started_at, Organization.name, Organization.slug, Perimeter.name,
            Model.model_version, Test.test_id, Test.prompt,
            func.avg(RunEvaluation.response_quality_score), func.avg(RunEvaluation.citation_quality_score),
            func.count(RunEvaluation.judge_model_id),
        )
        .select_from(runs)
        .join(RunResult, RunResult.run_id == runs.c.run_id)
        .join(Test, Test.test_id == RunResult.test_id)
        .join(Organization, Organization.id == runs.c.organization_id)
        .join(Model, Model.model_id == runs.c.tested_model_id)
        .outerjoin(Perimeter, Perimeter.id == runs.c.perimeter_id)
        .outerjoin(RunEvaluation, (RunEvaluation.run_id == runs.c.run_id) & (RunEvaluation.test_id == RunResult.test_id))
        .group_by(runs.c.run_id, runs.c.started_at, Organization.name, Organization.slug, Perimeter.name,
                  Model.model_version, Test.test_id, Test.prompt)
        .order_by(runs.c.run_id.desc(), Test.test_id)
        .limit(limit)
    )
    out = []
    for run_id, started, org_name, org_slug, peri, model, test_id, prompt, resp, cit, n in session.execute(stmt).all():
        out.append(dict(
            entite=org_name, perimetre=peri or "Sans périmètre", ia=model,
            question=prompt, question_id=test_id, evaluation=f"#{run_id}", run_id=run_id,
            date=started.strftime("%Y-%m-%d") if started else None,
            note_reponse=round(_f(resp), 2) if resp is not None else None,
            note_citations=round(_f(cit), 2) if cit is not None else None,
            notes=int(n), lien=f"/o/{org_slug}/runs/{run_id}",
        ))
    return out


def org_stats_summary(session: Session, org_id: int) -> dict[str, Any]:
    """Agrégats globaux d'une org pour les KPIs du tableau de bord (dsfr-data)."""
    row = session.execute(
        select(
            func.count(func.distinct(RunRow.run_id)).label("n_runs"),
            func.count(RunEvaluation.test_id).label("n_evals"),
            func.avg(RunEvaluation.response_quality_score).label("avg_response"),
            func.avg(RunEvaluation.citation_quality_score).label("avg_citation"),
            func.count(func.distinct(RunRow.tested_model_id)).label("n_models"),
        )
        .select_from(RunRow)
        .outerjoin(RunEvaluation, RunEvaluation.run_id == RunRow.run_id)
        .where(RunRow.organization_id == org_id)
    ).one()
    return dict(
        n_runs=int(row.n_runs),
        n_evals=int(row.n_evals),
        avg_response=_f(row.avg_response),
        avg_citation=_f(row.avg_citation),
        n_models=int(row.n_models),
    )


# -----------------------------
# Modèles (catalogue global — pas de filtre par org)
# -----------------------------
def list_models(session: Session, active_only: bool = True) -> list[Model]:
    stmt = select(Model).order_by(Model.model_id)
    if active_only:
        stmt = stmt.where(Model.is_active.is_(True))
    return list(session.execute(stmt).scalars().all())


def get_model(session: Session, model_id: int) -> Optional[Model]:
    return session.get(Model, model_id)


def model_run_refs(session: Session, model_id: int) -> int:
    """Nombre de références au modèle dans l'historique (runs testés + évaluations juge)."""
    n_runs = session.execute(
        select(func.count()).select_from(RunRow).where(RunRow.tested_model_id == model_id)
    ).scalar_one()
    n_evals = session.execute(
        select(func.count()).select_from(RunEvaluation).where(RunEvaluation.judge_model_id == model_id)
    ).scalar_one()
    return int(n_runs) + int(n_evals)


def create_model(
    session: Session,
    *,
    model_name: str,
    model_version: str,
    base_url: Optional[str],
    api_key: Optional[str],
    extra_headers: Optional[dict[str, Any]],
    search_config: Optional[dict[str, Any]] = None,
    hosting: Optional[str] = None,
    is_sovereign: bool = False,
) -> Model:
    model = Model(
        model_name=model_name,
        model_version=model_version,
        base_url=base_url or None,
        api_key=api_key or None,
        extra_headers=extra_headers or None,
        search_config=search_config or None,
        hosting=hosting or None,
        is_sovereign=bool(is_sovereign),
        is_active=True,
    )
    session.add(model)
    session.commit()
    return model


def update_model(
    session: Session,
    model_id: int,
    *,
    model_name: str,
    model_version: str,
    base_url: Optional[str],
    api_key: Optional[str],       # None = inchangée ; "" via clear_api_key
    clear_api_key: bool,
    extra_headers: Optional[dict[str, Any]],
    search_config: Optional[dict[str, Any]] = None,
    hosting: Optional[str] = None,
    is_sovereign: Optional[bool] = None,   # None = inchangé
) -> Model:
    model = session.get(Model, model_id)
    if model is None:
        raise ValueError(f"model_id={model_id} introuvable")
    model.model_name = model_name
    model.model_version = model_version
    model.base_url = base_url or None
    model.extra_headers = extra_headers or None
    model.search_config = search_config or None
    model.hosting = hosting or None
    if is_sovereign is not None:
        model.is_sovereign = bool(is_sovereign)
    if clear_api_key:
        model.api_key = None
    elif api_key:  # champ laissé vide = clé existante conservée
        model.api_key = api_key
    session.commit()
    return model


def set_model_active(session: Session, model_id: int, active: bool) -> None:
    model = session.get(Model, model_id)
    if model is None:
        raise ValueError(f"model_id={model_id} introuvable")
    model.is_active = active
    session.commit()


def toggle_model_judge(session: Session, model_id: int) -> bool:
    """Inverse le flag « juge » et renvoie la nouvelle valeur."""
    model = session.get(Model, model_id)
    if model is None:
        raise ValueError(f"model_id={model_id} introuvable")
    model.is_judge = not model.is_judge
    session.commit()
    return model.is_judge


def delete_model(session: Session, model_id: int) -> None:
    """Suppression réelle, refusée si l'historique y fait référence."""
    if model_run_refs(session, model_id):
        raise ValueError(
            "Ce modèle est référencé par des runs ou des évaluations : "
            "désactive-le plutôt (l'historique doit rester intact)."
        )
    model = session.get(Model, model_id)
    if model is None:
        raise ValueError(f"model_id={model_id} introuvable")
    session.delete(model)
    session.commit()


# -----------------------------
# Runs programmés
# -----------------------------
def list_schedules(
    session: Session, org_id: int, perimeter_id: Optional[int] = None
) -> list[ScheduledRun]:
    stmt = (
        select(ScheduledRun)
        .where(ScheduledRun.organization_id == org_id)
        .order_by(ScheduledRun.schedule_id)
    )
    if perimeter_id is not None:
        stmt = stmt.where(ScheduledRun.perimeter_id == perimeter_id)
    return list(session.execute(stmt).scalars().all())


def get_schedule(
    session: Session, org_id: int, schedule_id: int
) -> Optional[ScheduledRun]:
    sr = session.get(ScheduledRun, schedule_id)
    if sr is None or sr.organization_id != org_id:
        return None
    return sr


def create_schedule(
    session: Session,
    org_id: int,
    *,
    perimeter_id: int,
    name: str,
    tested_models: list[str],
    judges: list[dict[str, Any]],
    test_ids: Optional[list[int]],
    note: Optional[str],
    schedule_kind: str,
    schedule_config: dict[str, Any],
    next_run_at: datetime,
) -> ScheduledRun:
    sr = ScheduledRun(
        organization_id=org_id,
        perimeter_id=perimeter_id,
        name=name,
        tested_models=tested_models,
        judges=judges,
        test_ids=test_ids,
        note=note,
        schedule_kind=schedule_kind,
        schedule_config=schedule_config,
        enabled=True,
        next_run_at=next_run_at,
    )
    session.add(sr)
    session.commit()
    return sr


def set_schedule_enabled(
    session: Session,
    org_id: int,
    schedule_id: int,
    enabled: bool,
    next_run_at: Optional[datetime] = None,
) -> None:
    sr = get_schedule(session, org_id, schedule_id)
    if sr is None:
        raise ValueError(f"schedule_id={schedule_id} introuvable pour org={org_id}")
    sr.enabled = enabled
    if enabled and next_run_at is not None:
        sr.next_run_at = next_run_at
    session.commit()


def delete_schedule(session: Session, org_id: int, schedule_id: int) -> None:
    sr = get_schedule(session, org_id, schedule_id)
    if sr is None:
        raise ValueError(f"schedule_id={schedule_id} introuvable pour org={org_id}")
    session.delete(sr)
    session.commit()


# -----------------------------
# Tests
# -----------------------------
def list_tests(
    session: Session, org_id: int, perimeter_id: Optional[int] = None
) -> list[Test]:
    stmt = select(Test).where(Test.organization_id == org_id).order_by(Test.test_id)
    if perimeter_id is not None:
        stmt = stmt.where(Test.perimeter_id == perimeter_id)
    return list(session.execute(stmt).scalars().all())


def get_test(session: Session, org_id: int, test_id: int) -> Optional[Test]:
    test = session.get(Test, test_id)
    if test is None or test.organization_id != org_id:
        return None
    return test


# prompt_types de la seed : 1 = response_quality, 2 = citation_quality.
_PROMPT_TYPE_RESPONSE = 1
_PROMPT_TYPE_CITATION = 2


def default_prompt_ids(session: Session) -> tuple[Optional[int], Optional[int]]:
    """Grilles de notation par défaut : première grille de chaque type.

    L'évaluation exige les deux grilles (INNER JOIN dans evaluate_run) — une
    question sans grille passerait la phase RUN puis ferait planter le job.
    """
    resp = session.execute(
        select(func.min(EvaluationPrompt.prompt_id)).where(
            EvaluationPrompt.prompt_type_id == _PROMPT_TYPE_RESPONSE
        )
    ).scalar()
    cit = session.execute(
        select(func.min(EvaluationPrompt.prompt_id)).where(
            EvaluationPrompt.prompt_type_id == _PROMPT_TYPE_CITATION
        )
    ).scalar()
    return resp, cit


def create_test(
    session: Session,
    org_id: int,
    *,
    perimeter_id: int,
    prompt: str,
    expected_answer: Optional[str],
    response_quality_prompt_id: Optional[int],
    citation_quality_prompt_id: Optional[int],
    status: str = "published",
    created_by: Optional[int] = None,
) -> Test:
    """Crée une question, publiée par défaut ou en brouillon (E8). Validation métier
    requise (fin de E8) : « publiée » devient « en relecture », soumise par `created_by`."""
    if status not in ("draft", "published"):
        raise ValueError(f"Statut de création invalide : {status!r} (brouillon ou publiée).")
    from geoeval.web import reviews

    org = session.get(Organization, org_id)
    submit = status == "published" and org is not None and reviews.required(session, org)
    default_resp, default_cit = default_prompt_ids(session)
    test = Test(
        organization_id=org_id,
        perimeter_id=perimeter_id,
        prompt=prompt,
        expected_answer=expected_answer or None,
        response_quality_prompt_id=response_quality_prompt_id or default_resp,
        citation_quality_prompt_id=citation_quality_prompt_id or default_cit,
        validity_start_at=datetime.now(timezone.utc),
        validity_end_at=None,
        status="draft" if submit else status,
    )
    session.add(test)
    session.commit()
    if submit:
        reviews.mark_submitted(session, org, test, created_by, reason="nouvelle question")
    return test


def update_test(
    session: Session,
    org_id: int,
    test_id: int,
    *,
    prompt: str,
    expected_answer: Optional[str],
    response_quality_prompt_id: Optional[int],
    citation_quality_prompt_id: Optional[int],
    user_id: Optional[int] = None,
) -> Test:
    """Validation métier requise (fin de E8) : changer l'énoncé ou la réponse attendue d'une
    question publiée ou en relecture la (re)met en relecture, soumise par `user_id`."""
    test = get_test(session, org_id, test_id)
    if test is None:
        raise ValueError(f"test_id={test_id} introuvable pour org={org_id}")
    statement_changed = (prompt != test.prompt) or ((expected_answer or None) != test.expected_answer)
    default_resp, default_cit = default_prompt_ids(session)
    new_resp = response_quality_prompt_id or default_resp
    new_cit = citation_quality_prompt_id or default_cit
    if (new_resp, new_cit) != (test.response_quality_prompt_id, test.citation_quality_prompt_id):
        from geoeval.web import campaigns

        if test.test_id in campaigns.locked_test_ids(session):
            raise ValueError("Cette question appartient à une campagne active : sa grille de notation est figée "
                             "jusqu'à la clôture de la campagne.")
    test.prompt = prompt
    test.expected_answer = expected_answer or None
    test.response_quality_prompt_id = response_quality_prompt_id or default_resp
    test.citation_quality_prompt_id = citation_quality_prompt_id or default_cit
    session.commit()
    if statement_changed and test.status in ("published", "in_review"):
        from geoeval.web import reviews

        org = session.get(Organization, org_id)
        if reviews.required(session, org):
            reviews.mark_submitted(session, org, test, user_id, reason="énoncé ou réponse attendue modifié")
    return test


TEST_STATUS_LABELS = {"draft": "brouillon", "in_review": "en relecture", "published": "publiée", "retired": "retirée"}


def deactivate_test(session: Session, org_id: int, test_id: int) -> None:
    """Retire une question (E8 : statut « retirée ») ; l'historique est conservé."""
    test = get_test(session, org_id, test_id)
    if test is None:
        raise ValueError(f"test_id={test_id} introuvable pour org={org_id}")
    test.status = "retired"
    test.validity_end_at = datetime.now(timezone.utc)
    session.commit()


def reactivate_test(session: Session, org_id: int, test_id: int, user_id: Optional[int] = None) -> None:
    """Republie une question retirée (un brouillon se publie avec `publish_test`).
    Validation métier requise : la question repasse en relecture."""
    test = get_test(session, org_id, test_id)
    if test is None:
        raise ValueError(f"test_id={test_id} introuvable pour org={org_id}")
    if test.status in ("draft", "in_review"):
        raise ValueError("Seule une question retirée se réactive.")
    from geoeval.web import reviews

    org = session.get(Organization, org_id)
    if reviews.required(session, org):
        reviews.mark_submitted(session, org, test, user_id, reason="réactivation")
        return
    test.status = "published"
    test.validity_end_at = None
    session.commit()


def publish_test(session: Session, org_id: int, test_id: int, user_id: Optional[int] = None) -> Test:
    """Brouillon → publiée : la question entre dans les runs, pools et campagnes.
    Validation métier requise (fin de E8) : brouillon → en relecture (soumission)."""
    test = get_test(session, org_id, test_id)
    if test is None:
        raise ValueError(f"test_id={test_id} introuvable pour org={org_id}")
    if test.status != "draft":
        raise ValueError(f"Seule une question en brouillon se publie (statut actuel : {TEST_STATUS_LABELS[test.status]}).")
    from geoeval.web import reviews

    org = session.get(Organization, org_id)
    if reviews.required(session, org):
        return reviews.submit(session, org, test, user_id)
    test.status = "published"
    test.validity_start_at = datetime.now(timezone.utc)
    session.commit()
    return test


# -----------------------------
# Prompts d'évaluation (catalogue global)
# -----------------------------
def list_prompts(session: Session) -> list[dict[str, Any]]:
    stmt = (
        select(EvaluationPrompt, PromptType.prompt_type_label)
        .join(PromptType, PromptType.prompt_type_id == EvaluationPrompt.prompt_type_id)
        .order_by(EvaluationPrompt.prompt_id)
    )
    out = []
    for prompt, type_label in session.execute(stmt).all():
        out.append(
            dict(
                prompt_id=prompt.prompt_id,
                prompt_type_id=prompt.prompt_type_id,
                prompt_type_label=type_label,
                prompt_name=prompt.prompt_name,
                prompt_text=prompt.prompt_text,
            )
        )
    return out


def get_prompt(session: Session, prompt_id: int) -> Optional[EvaluationPrompt]:
    return session.get(EvaluationPrompt, prompt_id)


def list_prompt_types(session: Session) -> list[PromptType]:
    return list(
        session.execute(select(PromptType).order_by(PromptType.prompt_type_id)).scalars().all()
    )


def create_prompt(
    session: Session, *, prompt_type_id: int, prompt_name: str, prompt_text: str
) -> EvaluationPrompt:
    prompt = EvaluationPrompt(
        prompt_type_id=prompt_type_id,
        prompt_name=prompt_name,
        prompt_text=prompt_text,
    )
    session.add(prompt)
    session.commit()
    return prompt


def update_prompt(
    session: Session,
    prompt_id: int,
    *,
    prompt_type_id: int,
    prompt_name: str,
    prompt_text: str,
) -> EvaluationPrompt:
    prompt = session.get(EvaluationPrompt, prompt_id)
    if prompt is None:
        raise ValueError(f"prompt_id={prompt_id} introuvable")
    prompt.prompt_type_id = prompt_type_id
    prompt.prompt_name = prompt_name
    prompt.prompt_text = prompt_text
    session.commit()
    return prompt
