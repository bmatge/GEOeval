"""Parsing strict de la sortie des LLM-juges (evaluate.py)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from geoeval.core import evaluate
from geoeval.core.evaluate import (
    CONFORMITY_LABELS,
    JudgeResult,
    build_prompt_json_guardrails,
    parse_judge_output,
)


def test_parse_json_propre():
    r = parse_judge_output('{"label": "conforme", "score": 8}')
    assert r.label == "conforme"
    assert r.score == Decimal("8.00")


def test_parse_tolere_backticks_markdown():
    raw = '```json\n{"label": "partiel", "score": 5.5}\n```'
    r = parse_judge_output(raw)
    assert r.label == "partiel"
    assert r.score == Decimal("5.50")


def test_parse_tolere_prose_autour():
    raw = 'Voici mon évaluation : {"label": "non_conforme", "score": 2} merci.'
    assert parse_judge_output(raw).label == "non_conforme"


def test_score_quantise_a_deux_decimales():
    r = parse_judge_output('{"label": "x", "score": 7.456}')
    assert r.score == Decimal("7.46")


def test_score_chaine_numerique_acceptee():
    assert parse_judge_output('{"label": "x", "score": "9"}').score == Decimal("9.00")


@pytest.mark.parametrize("score", [-1, 10.01, "abc", None])
def test_score_hors_bornes_ou_non_numerique(score):
    import json

    with pytest.raises(ValueError):
        parse_judge_output(json.dumps({"label": "x", "score": score}))


@pytest.mark.parametrize("raw", ["", "   ", "pas de json ici", "[1, 2, 3]", '{"score": 5}', '{"label": "", "score": 5}'])
def test_sorties_invalides(raw):
    with pytest.raises(ValueError):
        parse_judge_output(raw)


def test_label_strip():
    assert parse_judge_output('{"label": "  conforme ", "score": 10}').label == "conforme"


def test_is_conformity():
    for lab in CONFORMITY_LABELS:
        assert JudgeResult(label=lab, score=Decimal("5")).is_conformity()
    assert not JudgeResult(label="libre", score=Decimal("5")).is_conformity()


def test_guardrails_rappellent_le_schema():
    out = build_prompt_json_guardrails("  Note cette réponse.  ")
    assert out.startswith("Note cette réponse.")
    assert '"label"' in out and '"score"' in out
    assert "SANS markdown" in out


# ---- Prompt du notateur de citations : les sources renvoyées à part comptent --------
def test_citations_structurees_transmises_au_notateur():
    answer = "Le loyer maximal est de 200 euros par mois [1][2]."
    prompt = evaluate.build_citation_user_prompt(
        "Tu évalues la QUALITÉ DES CITATIONS.", answer,
        ["https://www.exemple.gouv.fr/a", {"url": "https://www.exemple.gouv.fr/b", "title": "B"},
         "https://www.exemple.gouv.fr/a", "", None],
    )
    assert answer in prompt and "[Sources fournies par le modèle testé]" in prompt
    assert "[1] https://www.exemple.gouv.fr/a" in prompt and "[2] https://www.exemple.gouv.fr/b" in prompt
    assert prompt.count("exemple.gouv.fr/a") == 1, "sources dédoublonnées, ordre des renvois conservé"
    assert "renvois numérotés" in prompt
    assert prompt.index("Tu évalues") < prompt.index("=== DONNÉES À ÉVALUER ===") < prompt.index(answer)


def test_sans_source_structuree_le_notateur_le_sait():
    for raw in (None, [], "pas une liste", [{"title": "sans url"}]):
        prompt = evaluate.build_citation_user_prompt("Grille.", "Réponse sans source.", raw)
        assert "(aucune source renvoyée en dehors du texte)" in prompt and "[1]" not in prompt


def test_liste_de_sources_bornee():
    urls = [f"https://exemple.fr/{i}" for i in range(evaluate.MAX_CITATIONS_IN_PROMPT + 5)]
    prompt = evaluate.build_citation_user_prompt("Grille.", "Réponse.", urls)
    assert f"[{evaluate.MAX_CITATIONS_IN_PROMPT}] " in prompt and f"[{evaluate.MAX_CITATIONS_IN_PROMPT + 1}] " not in prompt
    assert "et 5 autres sources" in prompt
