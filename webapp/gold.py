"""
Import de gold annotations depuis CSV (ADR-079 §2).

Le CSV attendu :
    test_id,run_id,annotator_email,response_label,response_score,citation_label,citation_score[,notes]

Labels : conforme | partiel | non_conforme | hors_sujet.
Scores : 0.0–10.0.
"""
from __future__ import annotations

import csv
import io
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from evaluate import CONFORMITY_LABELS
from models import GoldAnnotation, RunRow, Test


REQUIRED_HEADERS = (
    "test_id", "run_id", "annotator_email",
    "response_label", "response_score",
    "citation_label", "citation_score",
)


def _validate_score(raw: str) -> Decimal:
    try:
        v = Decimal(str(raw).replace(",", "."))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"score non numérique: {raw!r}") from exc
    if v < 0 or v > 10:
        raise ValueError(f"score hors [0,10]: {v}")
    return v.quantize(Decimal("0.01"))


def _validate_label(raw: str, field: str) -> str:
    label = (raw or "").strip().lower()
    if label not in CONFORMITY_LABELS:
        raise ValueError(
            f"{field}={label!r} invalide (attendu: {', '.join(CONFORMITY_LABELS)})"
        )
    return label


def import_csv(session: Session, content: str | bytes) -> dict[str, Any]:
    """Parse + insère. Renvoie `{inserted, rejected: [{line, reason}]}`."""
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(content))
    if reader.fieldnames is None or not all(h in reader.fieldnames for h in REQUIRED_HEADERS):
        return dict(
            inserted=0,
            rejected=[
                dict(line=1, reason=f"Colonnes manquantes. Requis : {', '.join(REQUIRED_HEADERS)}")
            ],
        )

    inserted = 0
    rejected: list[dict[str, Any]] = []
    line_no = 1  # header
    for row in reader:
        line_no += 1
        try:
            test_id = int(row["test_id"])
            run_id = int(row["run_id"])
            annot = (row["annotator_email"] or "").strip().lower()
            if not annot:
                raise ValueError("annotator_email vide")
            resp_label = _validate_label(row["response_label"], "response_label")
            resp_score = _validate_score(row["response_score"])
            cit_label = _validate_label(row["citation_label"], "citation_label")
            cit_score = _validate_score(row["citation_score"])
            notes = (row.get("notes") or "").strip() or None

            # FK checks : test et run doivent exister.
            if not session.get(Test, test_id):
                raise ValueError(f"test_id={test_id} inconnu")
            if not session.get(RunRow, run_id):
                raise ValueError(f"run_id={run_id} inconnu")

            payload = dict(
                test_id=test_id, run_id=run_id, annotator_email=annot,
                response_label=resp_label, response_score=resp_score,
                citation_label=cit_label, citation_score=cit_score,
                notes=notes,
            )
            ins = insert(GoldAnnotation).values(**payload)
            upsert = ins.on_conflict_do_update(
                index_elements=[
                    GoldAnnotation.test_id, GoldAnnotation.run_id,
                    GoldAnnotation.annotator_email,
                ],
                set_={
                    "response_label": ins.excluded.response_label,
                    "response_score": ins.excluded.response_score,
                    "citation_label": ins.excluded.citation_label,
                    "citation_score": ins.excluded.citation_score,
                    "notes": ins.excluded.notes,
                },
            )
            session.execute(upsert)
            inserted += 1
        except Exception as exc:  # noqa: BLE001
            rejected.append(dict(line=line_no, reason=str(exc)))
    session.commit()
    return dict(inserted=inserted, rejected=rejected)
