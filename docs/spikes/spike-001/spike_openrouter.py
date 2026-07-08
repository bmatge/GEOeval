"""Spike EPIC-001 Phase 0 — OpenRouter : geo-targeting FR, routage Mistral, forme de usage.

Script JETABLE (cf. docs/epics/EPIC-001, S0.1/S0.2/S0.3). Aucun code de prod.
Nécessite OPENROUTER_API_KEY dans l'environnement ou dans le .env du repo GEOeval.

Usage :
    python3 spike_openrouter.py            # tout le spike
    python3 spike_openrouter.py s01        # geo-targeting seulement
    python3 spike_openrouter.py s02        # Mistral natif vs Exa
    python3 spike_openrouter.py s03        # forme de usage/coût

Chaque appel dumpe la réponse brute en JSON dans le dossier du script (spike_out_*.json).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

OUT_DIR = Path(__file__).parent
BASE_URL = "https://openrouter.ai/api/v1"

# Question dont la réponse dépend de la localisation : posée en ANGLAIS exprès,
# pour que seule la géoloc (pas la langue) puisse tirer les sources vers la France.
GEO_QUESTION = "What are the next public holidays, and on which dates?"
# Question de contrôle en français (biais de langue attendu vers des sources .fr).
GEO_QUESTION_FR = "Quels sont les prochains jours fériés, et à quelles dates ?"

FR_LOCATION = {
    "approximate": {"country": "FR", "city": "Paris", "timezone": "Europe/Paris"}
}


def _client():
    from openai import OpenAI

    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        env = Path(__file__).resolve().parents[0] / ".env"
        for candidate in (env, Path.home() / "Developer/GitHub/GEOeval/.env"):
            if candidate.exists():
                for line in candidate.read_text().splitlines():
                    if line.startswith("OPENROUTER_API_KEY="):
                        key = line.split("=", 1)[1].strip().strip('"')
    if not key:
        sys.exit("OPENROUTER_API_KEY introuvable (env ou .env du repo). Spike bloqué.")
    return OpenAI(
        base_url=BASE_URL,
        api_key=key,
        default_headers={
            "HTTP-Referer": "https://geoeval.lab.miweb.run",
            "X-Title": "GEOeval spike",
        },
    )


def _call(client, tag: str, model: str, question: str, *, plugins=None, web_search_options=None):
    """Un appel chat.completions + dump JSON complet, retourne (texte, citations, usage)."""
    extra_body: dict = {"usage": {"include": True}}
    if plugins is not None:
        extra_body["plugins"] = plugins
    if web_search_options is not None:
        extra_body["web_search_options"] = web_search_options
    print(f"\n=== [{tag}] {model} — extra_body={json.dumps(extra_body, ensure_ascii=False)}")
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": question}],
            extra_body=extra_body,
            timeout=180,
        )
    except Exception as exc:  # noqa: BLE001 — spike : on veut VOIR l'erreur brute
        print(f"    ERREUR: {type(exc).__name__}: {exc}")
        (OUT_DIR / f"spike_out_{tag}_error.txt").write_text(f"{type(exc).__name__}: {exc}")
        return None, [], None

    raw = resp.model_dump()
    (OUT_DIR / f"spike_out_{tag}.json").write_text(
        json.dumps(raw, indent=2, ensure_ascii=False, default=str)
    )
    msg = raw["choices"][0]["message"]
    annotations = msg.get("annotations") or []
    citations = [
        a["url_citation"]["url"]
        for a in annotations
        if a.get("type") == "url_citation" and a.get("url_citation", {}).get("url")
    ]
    domains = sorted({urlparse(u).netloc for u in citations})
    print(f"    réponse ({len(msg.get('content') or '')} car.) : {(msg.get('content') or '')[:200]!r}…")
    print(f"    citations ({len(citations)}) — domaines : {domains}")
    print(f"    usage : {json.dumps(raw.get('usage'), ensure_ascii=False, default=str)}")
    return msg.get("content"), citations, raw.get("usage")


def s01(client):
    """S0.1 — le geo-targeting FR est-il propagé via OpenRouter ?"""
    print("\n" + "#" * 70 + "\n# S0.1 — geo-targeting FR (question EN, seule la géoloc peut tirer vers .fr)\n" + "#" * 70)
    plugins_native = [{"id": "web", "engine": "native", "max_results": 5}]
    # a) sans localisation → baseline
    _call(client, "s01a_native_noloc", "openai/gpt-5.2", GEO_QUESTION, plugins=plugins_native)
    # b) web_search_options.user_location (mécanisme OpenAI-style) → propagé ?
    _call(
        client, "s01b_native_userloc", "openai/gpt-5.2", GEO_QUESTION,
        plugins=plugins_native,
        web_search_options={"search_context_size": "medium", "user_location": FR_LOCATION},
    )
    # c) contournement candidat si (b) échoue : search_prompt orienté France
    _call(
        client, "s01c_native_searchprompt", "openai/gpt-5.2", GEO_QUESTION,
        plugins=[{"id": "web", "engine": "native", "max_results": 5,
                  "search_prompt": "The user is located in Paris, France. Prioritize French sources."}],
    )
    # d) contrôle : question FR sans localisation (biais de langue seul)
    _call(client, "s01d_native_frquestion", "openai/gpt-5.2", GEO_QUESTION_FR, plugins=plugins_native)
    # e) même test sur Gemini (2e modèle testé du benchmark)
    _call(
        client, "s01e_gemini_userloc", "google/gemini-2.5-pro", GEO_QUESTION,
        plugins=plugins_native,
        web_search_options={"user_location": FR_LOCATION},
    )


def s02(client):
    """S0.2 — Mistral : natif ou retombée Exa ?"""
    print("\n" + "#" * 70 + "\n# S0.2 — Mistral via OpenRouter : natif vs Exa\n" + "#" * 70)
    # a) engine par défaut (auto) → sur quel moteur ça route ?
    _call(client, "s02a_mistral_auto", "mistralai/mistral-large-2512", GEO_QUESTION_FR,
          plugins=[{"id": "web", "max_results": 5}])
    # b) engine:native forcé → erreur explicite ou fallback silencieux ?
    _call(client, "s02b_mistral_native", "mistralai/mistral-large-2512", GEO_QUESTION_FR,
          plugins=[{"id": "web", "engine": "native", "max_results": 5}])


def s03(client):
    """S0.3 — forme exacte de usage / coût réel (mapping vers UsageRecord)."""
    print("\n" + "#" * 70 + "\n# S0.3 — structure usage/coût (usage.include=true)\n" + "#" * 70)
    # Appel minimal SANS web search (coût de base) — le moins cher possible.
    _call(client, "s03a_usage_plain", "mistralai/ministral-3b-2512", "Réponds simplement : OK")
    # Un appel avec web search a déjà été fait en s01a/s02a → comparer les champs
    # cost / cost_details entre spike_out_s03a et spike_out_s01a pour isoler le coût search.
    print("\n→ comparer usage de s03a (sans search) vs s01a/s02a (avec search) dans les JSON dumps.")


if __name__ == "__main__":
    steps = sys.argv[1:] or ["s01", "s02", "s03"]
    c = _client()
    for step in steps:
        {"s01": s01, "s02": s02, "s03": s03}[step](c)
    print(f"\nDumps JSON écrits dans {OUT_DIR}/spike_out_*.json")


def s04(client):
    """S0.1-bis — recherche FORCÉE (météo : impossible sans search, geo-dépendante)."""
    print("\n" + "#" * 70 + "\n# S0.4 — recherche forcée (météo demain) : geo + citations réelles\n" + "#" * 70)
    q = "What is the weather forecast for tomorrow where I am?"
    plugins_native = [{"id": "web", "engine": "native", "max_results": 5}]
    # a) gpt-5.2 natif, sans localisation → où croit-il qu'on est ?
    _call(client, "s04a_gpt_weather_noloc", "openai/gpt-5.2", q, plugins=plugins_native)
    # b) gpt-5.2 natif + user_location Paris/FR → propagé ?
    _call(
        client, "s04b_gpt_weather_userloc", "openai/gpt-5.2", q,
        plugins=plugins_native,
        web_search_options={"user_location": FR_LOCATION},
    )
    # c) gemini-2.5-pro engine auto (natif refusé en 404) → quel moteur, quelles citations ?
    _call(client, "s04c_gemini_weather_auto", "google/gemini-2.5-pro",
          "Quel temps fera-t-il demain à Paris ?", plugins=[{"id": "web", "max_results": 5}])
