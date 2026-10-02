"""Helpers purs de llm_clients.py : familles, OpenRouter, retry."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from geoeval.core import llm_clients
from geoeval.core.llm_clients import (
    LLMCallError,
    _non_retryable_reason,
    call_with_retry,
    citations_from_openrouter_message,
    openrouter_web_extra_body,
    provider_family,
    usage_from_openrouter_response,
)


@pytest.mark.parametrize(
    "name,family",
    [
        ("openai", "openai"), ("ChatGPT", "openai"), ("gpt", "openai"),
        ("albert", "albert"), ("Etalab", "albert"),
        ("openai-compatible", "generic"),
        ("mistral", "mistral"), ("MistralAI", "mistral"),
        ("gemini", "gemini"), ("google", "gemini"),
        ("openrouter", "openrouter"),
        ("inconnu", None), ("", None), (None, None),
    ],
)
def test_provider_family(name, family):
    assert provider_family(name) == family


def test_citations_dedoublonnees_dicts_et_objets():
    msg = SimpleNamespace(
        annotations=[
            {"type": "url_citation", "url_citation": {"url": "https://a.fr"}},
            {"type": "url_citation", "url_citation": {"url": "https://b.fr"}},
            {"type": "url_citation", "url_citation": {"url": "https://a.fr"}},  # doublon
            {"type": "autre", "url_citation": {"url": "https://ignore.fr"}},
            SimpleNamespace(type="url_citation", url_citation=SimpleNamespace(url="https://c.fr")),
            SimpleNamespace(type="url_citation", url_citation=None),
        ]
    )
    assert citations_from_openrouter_message(msg) == ["https://a.fr", "https://b.fr", "https://c.fr"]


def test_citations_sans_annotations():
    assert citations_from_openrouter_message(SimpleNamespace()) == []
    assert citations_from_openrouter_message(SimpleNamespace(annotations=None)) == []


def test_extra_body_sans_recherche():
    assert openrouter_web_extra_body(None) == {"usage": {"include": True}}
    assert openrouter_web_extra_body({"engine": "off"}) == {"usage": {"include": True}}


def test_extra_body_exa_complet():
    body = openrouter_web_extra_body(
        {"engine": "Exa", "max_results": "5", "allowed_domains": ("service-public.fr",),
         "search_context_size": "high"}
    )
    assert body["plugins"] == [
        {"id": "web", "engine": "exa", "max_results": 5, "include_domains": ["service-public.fr"]}
    ]
    assert body["web_search_options"] == {"search_context_size": "high"}
    assert body["usage"] == {"include": True}


def test_usage_depuis_reponse():
    resp = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30, cost=0.0042))
    u = usage_from_openrouter_response(resp)
    assert (u.input_tokens, u.output_tokens, u.cost_usd) == (120, 30, Decimal("0.0042"))


def test_usage_cost_dans_model_extra():
    usage = SimpleNamespace(prompt_tokens=1, completion_tokens=2, cost=None, model_extra={"cost": "0.5"})
    assert usage_from_openrouter_response(SimpleNamespace(usage=usage)).cost_usd == Decimal("0.5")


def test_usage_absent():
    assert usage_from_openrouter_response(SimpleNamespace(usage=None)) is None


class _HttpError(Exception):
    def __init__(self, status_code: int, msg: str = "boom"):
        super().__init__(msg)
        self.status_code = status_code


class _GenaiError(Exception):
    def __init__(self, code: int):
        super().__init__("genai")
        self.code = code


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_non_retryable_codes(code):
    assert _non_retryable_reason(_HttpError(code)) is not None
    assert _non_retryable_reason(_GenaiError(code)) is not None


def test_429_simple_est_transitoire():
    assert _non_retryable_reason(_HttpError(429, "Rate limit exceeded, retry in 2s")) is None


def test_429_quota_dur_non_retryable():
    reason = _non_retryable_reason(_HttpError(429, "You exceeded your quota, check your plan and billing"))
    assert reason is not None and "quota" in reason


def test_500_et_sans_code_transitoires():
    assert _non_retryable_reason(_HttpError(500)) is None
    assert _non_retryable_reason(RuntimeError("réseau")) is None


@pytest.fixture()
def no_sleep(monkeypatch):
    monkeypatch.setattr(llm_clients, "_sleep_with_jitter", lambda s: None)
    monkeypatch.setattr(llm_clients.time, "sleep", lambda s: None)


def test_retry_puis_succes(no_sleep):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _HttpError(503)
        return "ok"

    assert call_with_retry(fn, retry_exceptions=(Exception,), max_retries=5) == "ok"
    assert calls["n"] == 3


def test_retry_epuise_releve_la_derniere_erreur(no_sleep):
    def fn():
        raise _HttpError(503, "indispo")

    with pytest.raises(_HttpError):
        call_with_retry(fn, retry_exceptions=(Exception,), max_retries=3)


def test_erreur_non_transitoire_fail_fast(no_sleep):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _HttpError(401, "invalid api key")

    with pytest.raises(LLMCallError):
        call_with_retry(fn, retry_exceptions=(Exception,), max_retries=8)
    assert calls["n"] == 1


def test_exception_hors_perimetre_non_retryee(no_sleep):
    def fn():
        raise KeyError("x")

    with pytest.raises(KeyError):
        call_with_retry(fn, retry_exceptions=(ValueError,), max_retries=8)
