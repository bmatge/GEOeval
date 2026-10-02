"""Variables de contexte propagées dans les logs et les métriques."""
from __future__ import annotations

import contextvars
from typing import Optional

request_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("geoeval_request_id", default=None)
job_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("geoeval_job_id", default=None)
org_id_var: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar("geoeval_org_id", default=None)
llm_family_var: contextvars.ContextVar[str] = contextvars.ContextVar("geoeval_llm_family", default="unknown")


def current_context() -> dict[str, object]:
    """Champs de contexte non nuls, pour les logs."""
    out: dict[str, object] = {}
    if (rid := request_id_var.get()) is not None:
        out["request_id"] = rid
    if (jid := job_id_var.get()) is not None:
        out["job_id"] = jid
    if (oid := org_id_var.get()) is not None:
        out["org_id"] = oid
    return out
