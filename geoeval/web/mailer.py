"""
Envoi d'emails par SMTP (bibliothèque standard, sans dépendance) — ADR-089 §2.8.

Inactif tant que GEOEVAL_SMTP_HOST n'est pas posé : les alertes restent visibles
dans l'application et l'envoi est noté « not_configured ». Configuration :

    GEOEVAL_SMTP_HOST, GEOEVAL_SMTP_PORT (587), GEOEVAL_SMTP_USER, GEOEVAL_SMTP_PASSWORD,
    GEOEVAL_SMTP_FROM, GEOEVAL_SMTP_STARTTLS (1), GEOEVAL_SMTP_SSL (0),
    GEOEVAL_PUBLIC_URL (base des liens dans les emails, ex. https://geoeval.lab.miweb.run)
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage
from typing import Iterable

logger = logging.getLogger("geoeval.web.mailer")

STATUS_SENT = "sent"
STATUS_NOT_CONFIGURED = "not_configured"
STATUS_NO_RECIPIENT = "no_recipient"
STATUS_FAILED = "failed"


def _flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes")


def is_configured() -> bool:
    return bool(os.environ.get("GEOEVAL_SMTP_HOST", "").strip())


def public_url(path: str = "") -> str:
    base = os.environ.get("GEOEVAL_PUBLIC_URL", "").strip().rstrip("/")
    return f"{base}{path}" if base else path


def build_message(to: list[str], subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = os.environ.get("GEOEVAL_SMTP_FROM", "").strip() or "geoeval@localhost"
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)
    return msg


def send(to: Iterable[str], subject: str, body: str) -> str:
    """Envoie un email ; renvoie un statut (sent / not_configured / no_recipient / failed).
    Ne lève jamais : une alerte ne doit pas casser un job."""
    recipients = sorted({r.strip() for r in to if r and r.strip()})
    if not recipients:
        return STATUS_NO_RECIPIENT
    if not is_configured():
        logger.info("SMTP non configuré : email « %s » non envoyé (%d destinataire(s))", subject, len(recipients))
        return STATUS_NOT_CONFIGURED
    host = os.environ["GEOEVAL_SMTP_HOST"].strip()
    port = int(os.environ.get("GEOEVAL_SMTP_PORT", "587"))
    user = os.environ.get("GEOEVAL_SMTP_USER", "").strip()
    password = os.environ.get("GEOEVAL_SMTP_PASSWORD", "")
    msg = build_message(recipients, subject, body)
    try:
        if _flag("GEOEVAL_SMTP_SSL", "0"):
            client = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            client = smtplib.SMTP(host, port, timeout=15)
        with client as smtp:
            if not _flag("GEOEVAL_SMTP_SSL", "0") and _flag("GEOEVAL_SMTP_STARTTLS", "1"):
                smtp.starttls()
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
        logger.info("email « %s » envoyé à %d destinataire(s)", subject, len(recipients))
        return STATUS_SENT
    except Exception:  # noqa: BLE001
        logger.exception("échec d'envoi de l'email « %s »", subject)
        return STATUS_FAILED
