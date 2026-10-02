"""Calcul des prochaines échéances (webapp/scheduler.py), heures de Paris → UTC."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from webapp.scheduler import compute_next_run, describe_schedule

# Jeudi 15 janvier 2026, 10:00 UTC = 11:00 Paris (hiver, UTC+1).
WINTER = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
# Mercredi 15 juillet 2026, 10:00 UTC = 12:00 Paris (été, UTC+2).
SUMMER = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)


def test_daily_plus_tard_le_meme_jour():
    assert compute_next_run("daily", {"time": "12:00"}, after=WINTER) == datetime(2026, 1, 15, 11, 0, tzinfo=timezone.utc)


def test_daily_deja_passe_bascule_au_lendemain():
    assert compute_next_run("daily", {"time": "09:00"}, after=WINTER) == datetime(2026, 1, 16, 8, 0, tzinfo=timezone.utc)


def test_daily_heure_d_ete():
    assert compute_next_run("daily", {"time": "09:00"}, after=SUMMER) == datetime(2026, 7, 16, 7, 0, tzinfo=timezone.utc)


def test_weekly_prochain_lundi():
    # weekday 0 = lundi → lundi 19 janvier 09:00 Paris = 08:00 UTC
    assert compute_next_run("weekly", {"weekday": 0, "time": "09:00"}, after=WINTER) == datetime(2026, 1, 19, 8, 0, tzinfo=timezone.utc)


def test_weekly_meme_jour_heure_passee_saute_une_semaine():
    # jeudi = 3, 09:00 déjà passé (il est 11:00 Paris) → jeudi suivant
    assert compute_next_run("weekly", {"weekday": 3, "time": "09:00"}, after=WINTER) == datetime(2026, 1, 22, 8, 0, tzinfo=timezone.utc)


def test_weekly_meme_jour_heure_future():
    assert compute_next_run("weekly", {"weekday": "3", "time": "18:30"}, after=WINTER) == datetime(2026, 1, 15, 17, 30, tzinfo=timezone.utc)


def test_once_futur_converti_depuis_paris():
    assert compute_next_run("once", {"at": "2026-01-20T08:00"}, after=WINTER) == datetime(2026, 1, 20, 7, 0, tzinfo=timezone.utc)


def test_once_passe_renvoie_none():
    assert compute_next_run("once", {"at": "2026-01-10T08:00"}, after=WINTER) is None


def test_every_n_hours():
    assert compute_next_run("every_n_hours", {"hours": "6"}, after=WINTER) == WINTER + timedelta(hours=6)


def test_kind_inconnu():
    with pytest.raises(ValueError):
        compute_next_run("monthly", {}, after=WINTER)


def test_resultats_tz_aware_utc():
    for kind, cfg in [("daily", {"time": "00:00"}), ("weekly", {"weekday": 6, "time": "23:59"}), ("every_n_hours", {"hours": 1})]:
        out = compute_next_run(kind, cfg, after=SUMMER)
        assert out.tzinfo is not None and out.utcoffset() == timedelta(0)
        assert out > SUMMER


def test_describe_schedule():
    assert describe_schedule("daily", {"time": "09:00"}) == "chaque jour à 09:00"
    assert describe_schedule("weekly", {"weekday": 4, "time": "08:15"}) == "chaque vendredi à 08:15"
    assert describe_schedule("once", {"at": "2026-01-20T08:00"}) == "une fois, le 2026-01-20 à 08:00"
    assert describe_schedule("every_n_hours", {"hours": 6}) == "toutes les 6 h"
