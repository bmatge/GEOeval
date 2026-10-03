"""Budget consolidé et alertes (E3) — règles pures et envoi d'email, sans base."""
from __future__ import annotations

from decimal import Decimal

import pytest

from geoeval.db.models import Organization
from geoeval.web import budget, budget_alerts, mailer

ORG = Organization(id=1, name="Ministère", slug="min", path="/1/", depth=0)


def _c(spent, cap, period="month"):
    return budget.Constraint(ORG, period, Decimal(cap), Decimal(spent))


@pytest.mark.parametrize("spent,cap,level,crossed", [
    ("0", "10", "ok", []),
    ("7.99", "10", "ok", []),
    ("8", "10", "warning", [80]),
    ("9.5", "10", "warning", [80]),
    ("10", "10", "exceeded", [80, 100]),
    ("12", "10", "exceeded", [80, 100]),
    ("0", "0", "exceeded", []),
    ("1", "0", "exceeded", [100]),
])
def test_niveaux_et_seuils(spent, cap, level, crossed):
    c = _c(spent, cap)
    assert c.level == level
    assert budget_alerts.thresholds_crossed(c) == crossed


def test_pourcentage_et_libelles():
    c = _c("4.5", "10", "day")
    assert c.pct == 45 and c.period_label == "journalier"
    assert _c("1", "0").ratio == Decimal("1") and _c("0", "0").ratio is None


def test_message_d_alerte():
    subject, body = budget_alerts._compose(_c("10", "10"), 100)
    assert "atteint" in subject and "Ministère" in subject
    assert "10.00 €" in body and "/o/min/budget" in body
    subject, _ = budget_alerts._compose(_c("8", "10"), 80)
    assert "à 80 %" in subject


def test_email_non_configure_et_sans_destinataire(monkeypatch):
    monkeypatch.delenv("GEOEVAL_SMTP_HOST", raising=False)
    assert mailer.send(["a@x.fr"], "s", "b") == mailer.STATUS_NOT_CONFIGURED
    assert mailer.send([], "s", "b") == mailer.STATUS_NO_RECIPIENT
    assert mailer.send(["  "], "s", "b") == mailer.STATUS_NO_RECIPIENT


class _FakeSMTP:
    sent: list = []
    calls: list = []

    def __init__(self, host, port, timeout=None):
        _FakeSMTP.calls.append(("connect", host, port))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        _FakeSMTP.calls.append(("starttls",))

    def login(self, user, password):
        _FakeSMTP.calls.append(("login", user))

    def send_message(self, msg):
        _FakeSMTP.sent.append(msg)


def test_email_envoye_par_smtp(monkeypatch):
    _FakeSMTP.sent, _FakeSMTP.calls = [], []
    monkeypatch.setattr(mailer.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setenv("GEOEVAL_SMTP_HOST", "smtp.test")
    monkeypatch.setenv("GEOEVAL_SMTP_PORT", "2525")
    monkeypatch.setenv("GEOEVAL_SMTP_USER", "u")
    monkeypatch.setenv("GEOEVAL_SMTP_FROM", "geoeval@test.gouv.fr")
    assert mailer.send(["b@x.fr", "a@x.fr", "a@x.fr"], "Sujet", "Corps") == mailer.STATUS_SENT
    msg = _FakeSMTP.sent[-1]
    assert msg["To"] == "a@x.fr, b@x.fr" and msg["From"] == "geoeval@test.gouv.fr" and msg["Subject"] == "Sujet"
    assert ("connect", "smtp.test", 2525) in _FakeSMTP.calls and ("starttls",) in _FakeSMTP.calls and ("login", "u") in _FakeSMTP.calls


def test_echec_smtp_ne_leve_pas(monkeypatch):
    class Boom(_FakeSMTP):
        def send_message(self, msg):
            raise OSError("connexion refusée")

    monkeypatch.setattr(mailer.smtplib, "SMTP", Boom)
    monkeypatch.setenv("GEOEVAL_SMTP_HOST", "smtp.test")
    assert mailer.send(["a@x.fr"], "s", "b") == mailer.STATUS_FAILED
