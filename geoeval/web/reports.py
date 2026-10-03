"""
Signalements humains sur les questions (ADR-089 §2.8, suite E7).

Arbitrage : **tout membre signale, le propriétaire traite**. Un membre d'une entité qui
voit une question peut la signaler (réponse attendue douteuse, question ambiguë ou
obsolète, citation hors sujet, autre) avec un commentaire. Une question est « vue »
par une entité si elle lui appartient, si un pool abonné à l'un de ses périmètres la
lui apporte, si elle figure au protocole d'une campagne à laquelle l'entité participe,
ou si elle apparaît dans l'un de ses runs.

Les éditeurs et administrateurs de l'entité propriétaire sont notifiés
(`report_opened`) et clôturent le signalement — corrigé ou rejeté — avec une réponse,
notifiée à son auteur (`report_resolved`). Un signalement ne modifie jamais la question
ni un résultat : la correction éventuelle se fait par les voies habituelles.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from geoeval.db.models import (
    Campaign,
    CampaignParticipant,
    Organization,
    Perimeter,
    RunResult,
    RunRow,
    Test,
    TestReport,
    User,
)
from geoeval.web import notifications, pools

CATEGORIES = {
    "expected_answer": "Réponse attendue douteuse",
    "ambiguous": "Question ambiguë",
    "obsolete": "Question obsolète",
    "citation": "Citation hors sujet",
    "other": "Autre",
}
STATUS_LABELS = {"open": "ouvert", "fixed": "corrigé", "rejected": "rejeté"}
MAX_COMMENT = 2000


class ReportError(ValueError):
    """Opération refusée sur un signalement. `status` : 400, 403, 404, 409."""

    def __init__(self, detail: str, status: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status


# ---------------------------------------------------------------------
# Visibilité d'une question pour une entité
# ---------------------------------------------------------------------
def can_see_test(session: Session, org: Organization, test: Test) -> bool:
    if test.organization_id == org.id:
        return True
    in_runs = session.execute(
        select(exists().where(RunResult.test_id == test.test_id, RunResult.run_id == RunRow.run_id,
                              RunRow.organization_id == org.id))
    ).scalar()
    if in_runs:
        return True
    protocols = session.execute(
        select(Campaign.protocol).join(CampaignParticipant, CampaignParticipant.campaign_id == Campaign.id)
        .where(CampaignParticipant.organization_id == org.id, Campaign.status != "draft")
    ).scalars().all()
    if any(test.test_id in {int(i) for i in (p or {}).get("test_ids", [])} for p in protocols):
        return True
    for peri in session.execute(select(Perimeter).where(Perimeter.organization_id == org.id)).scalars():
        if test.test_id in pools.effective_resolution(session, peri).test_ids:
            return True
    return False


# ---------------------------------------------------------------------
# Création, clôture
# ---------------------------------------------------------------------
def create(
    session: Session, org: Organization, *, test_id: int, category: str, comment: str,
    reporter_user_id: Optional[int], run_id: Optional[int] = None,
) -> TestReport:
    test = session.get(Test, test_id)
    if test is None or not can_see_test(session, org, test):
        raise ReportError("Question introuvable.", 404)
    if category not in CATEGORIES:
        raise ReportError(f"Motif inconnu : {category!r}.")
    comment = (comment or "").strip()
    if category == "other" and not comment:
        raise ReportError("Précise le problème en commentaire pour le motif « Autre ».")
    if len(comment) > MAX_COMMENT:
        raise ReportError(f"Commentaire limité à {MAX_COMMENT} caractères.")
    if run_id is not None:
        ok = session.execute(
            select(exists().where(RunRow.run_id == run_id, RunRow.organization_id == org.id,
                                  RunResult.run_id == RunRow.run_id, RunResult.test_id == test_id))
        ).scalar()
        if not ok:
            raise ReportError("Ce run n'appartient pas à l'entité ou ne contient pas cette question.")
    r = TestReport(test_id=test_id, owner_org_id=test.organization_id, reporter_org_id=org.id,
                   reporter_user_id=reporter_user_id, run_id=run_id, category=category, comment=comment)
    session.add(r)
    session.commit()
    owner = session.get(Organization, test.organization_id)
    excerpt = test.prompt if len(test.prompt) <= 120 else test.prompt[:117] + "…"
    origin = "" if owner.id == org.id else f" par « {org.name} »"
    notifications.notify(
        session, owner, "report_opened",
        title=f"Signalement : {CATEGORIES[category].lower()}{origin}",
        body=f"Question #{test_id} « {excerpt} »." + (f" Commentaire : {comment}" if comment else ""),
        link=f"/o/{owner.slug}/reports/{r.id}",
        payload={"report_id": r.id, "test_id": test_id, "category": category, "reporter_org_id": org.id},
        dedup_key=f"report_opened:{r.id}",
    )
    return r


def resolve(
    session: Session, report: TestReport, *, status: str, comment: str, resolved_by: Optional[int],
) -> TestReport:
    if report.status != "open":
        raise ReportError("Signalement déjà traité.", 409)
    if status not in ("fixed", "rejected"):
        raise ReportError("Issue attendue : corrigé ou rejeté.")
    comment = (comment or "").strip()
    if not comment:
        raise ReportError("Réponds à l'auteur du signalement (commentaire obligatoire).")
    report.status, report.resolution_comment = status, comment[:MAX_COMMENT]
    report.resolved_by, report.resolved_at = resolved_by, datetime.now(timezone.utc)
    session.commit()
    reporter = session.get(User, report.reporter_user_id) if report.reporter_user_id else None
    reporter_org = session.get(Organization, report.reporter_org_id)
    if reporter is not None:
        notifications.notify_users(
            session, [reporter], "report_resolved", org=reporter_org,
            title=f"Signalement {STATUS_LABELS[status]} — question #{report.test_id}",
            body=f"Réponse de l'entité propriétaire : {report.resolution_comment}",
            link=f"/o/{reporter_org.slug}/reports/{report.id}",
            payload={"report_id": report.id, "status": status}, dedup_key=f"report_resolved:{report.id}",
        )
    return report


# ---------------------------------------------------------------------
# Lecture
# ---------------------------------------------------------------------
def get_for_org(session: Session, org: Organization, report_id: int) -> tuple[TestReport, bool]:
    """Signalement visible de l'entité propriétaire ou de l'entité auteure ; (signalement, propriétaire ?)."""
    r = session.get(TestReport, report_id)
    if r is None or org.id not in (r.owner_org_id, r.reporter_org_id):
        raise ReportError("Signalement introuvable.", 404)
    return r, r.owner_org_id == org.id


def list_received(session: Session, org: Organization, *, status: Optional[str] = None) -> list[TestReport]:
    stmt = select(TestReport).where(TestReport.owner_org_id == org.id)
    if status:
        stmt = stmt.where(TestReport.status == status)
    return list(session.execute(stmt.order_by(TestReport.status != "open", TestReport.created_at.desc())).scalars())


def list_sent(session: Session, org: Organization) -> list[TestReport]:
    return list(session.execute(
        select(TestReport).where(TestReport.reporter_org_id == org.id, TestReport.owner_org_id != org.id)
        .order_by(TestReport.created_at.desc())
    ).scalars())


def open_counts(session: Session, test_ids: list[int]) -> dict[int, int]:
    if not test_ids:
        return {}
    rows = session.execute(
        select(TestReport.test_id, func.count()).where(TestReport.test_id.in_(test_ids), TestReport.status == "open")
        .group_by(TestReport.test_id)
    ).all()
    return {t: int(n) for t, n in rows}


def open_count_for_org(session: Session, org_id: int) -> int:
    return int(session.execute(
        select(func.count()).select_from(TestReport)
        .where(TestReport.owner_org_id == org_id, TestReport.status == "open")
    ).scalar_one())


def context(session: Session, reports: list[TestReport]) -> dict:
    """Questions, entités et auteurs nécessaires à l'affichage d'une liste de signalements."""
    test_ids = {r.test_id for r in reports}
    org_ids = {r.owner_org_id for r in reports} | {r.reporter_org_id for r in reports}
    user_ids = {u for r in reports for u in (r.reporter_user_id, r.resolved_by) if u}
    return dict(
        tests={t.test_id: t for t in session.execute(select(Test).where(Test.test_id.in_(test_ids or [-1]))).scalars()},
        orgs={o.id: o for o in session.execute(select(Organization).where(Organization.id.in_(org_ids or [-1]))).scalars()},
        users={u.id: u for u in session.execute(select(User).where(User.id.in_(user_ids or [-1]))).scalars()},
    )

