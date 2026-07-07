# llm_clients.py
from __future__ import annotations

import os
import random
import time
from typing import Callable, Optional, Tuple, Type, Any, TYPE_CHECKING

from dotenv import load_dotenv

# NB : les SDK LLM (openai, mistralai, google-genai) sont importés PARESSEUSEMENT
# dans les fonctions de création de client ci-dessous. On peut ainsi n'installer
# que les SDK des providers réellement utilisés (ex. Mistral + Gemini sans OpenAI).

load_dotenv()

if TYPE_CHECKING:  # pour les annotations uniquement (jamais exécuté au runtime)
    from openai import OpenAI
    from mistralai import Mistral
    from google import genai

# -----------------------------
# Client singletons (process-wide)
# -----------------------------
_OPENAI_CLIENT_SINGLETON: Optional[OpenAI] = None
_MISTRAL_CLIENT_SINGLETON: Optional[Mistral] = None
_GEMINI_CLIENT_SINGLETON: Optional[genai.Client] = None


def get_openai_client_singleton() -> "OpenAI":
    global _OPENAI_CLIENT_SINGLETON
    if _OPENAI_CLIENT_SINGLETON is None:
        from openai import OpenAI
        _OPENAI_CLIENT_SINGLETON = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return _OPENAI_CLIENT_SINGLETON


def get_mistral_client_singleton() -> "Mistral":
    global _MISTRAL_CLIENT_SINGLETON
    if _MISTRAL_CLIENT_SINGLETON is None:
        try:
            from mistralai import Mistral  # SDK v1.x
        except ImportError:
            from mistralai.client import Mistral  # SDK v2.x (namespace package)
        _MISTRAL_CLIENT_SINGLETON = Mistral(api_key=os.environ["MISTRAL_API_KEY"])
    return _MISTRAL_CLIENT_SINGLETON


def get_gemini_client_singleton() -> "genai.Client":
    global _GEMINI_CLIENT_SINGLETON
    if _GEMINI_CLIENT_SINGLETON is None:
        from google import genai
        _GEMINI_CLIENT_SINGLETON = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _GEMINI_CLIENT_SINGLETON


# -----------------------------
# Mistral agent singleton (par model_version)
# -----------------------------
_MISTRAL_AGENT_SINGLETON_BY_MODEL_VERSION: dict[str, Any] = {}


def get_mistral_agent_singleton_by_model_version(
    *,
    client: Mistral,
    model_version: str,
    instructions: str,
    tools: Optional[list[dict]] = None,
    completion_args: Optional[dict] = None,
    name: str = "GEOeval Agent",
    description: str = "Agent singleton for GEOeval",
):
    """
    Retourne un agent Mistral singleton *par model_version* (créé une seule fois par exécution).
    Renvoie l'objet agent (qui possède agent.id).
    """
    global _MISTRAL_AGENT_SINGLETON_BY_MODEL_VERSION

    if model_version not in _MISTRAL_AGENT_SINGLETON_BY_MODEL_VERSION:
        agent = client.beta.agents.create(
            model=model_version,
            name=name,
            description=description,
            instructions=instructions,
            tools=tools or [],
            completion_args=completion_args or {"temperature": 0.8, "top_p": 1},
        )
        _MISTRAL_AGENT_SINGLETON_BY_MODEL_VERSION[model_version] = agent

    return _MISTRAL_AGENT_SINGLETON_BY_MODEL_VERSION[model_version]


# -----------------------------
# Retry / throttle helpers
# -----------------------------
def _sleep_with_jitter(seconds: float) -> None:
    # jitter 70%-130%
    seconds = seconds * (0.7 + 0.6 * random.random())
    time.sleep(seconds)


def call_with_retry(
    fn: Callable[[], str],
    *,
    retry_exceptions: Tuple[Type[BaseException], ...],
    max_retries: int = 8,
    base_sleep: float = 1.0,
    max_sleep: float = 30.0,
    success_delay: float = 0.2,
) -> str:
    """
    Exécute fn() avec retry backoff exponentiel + jitter sur certaines exceptions.
    Ajoute un petit délai fixe après succès (throttle soft).
    """
    last_exc: Optional[BaseException] = None

    for attempt in range(max_retries):
        try:
            out = fn()
            if success_delay > 0:
                time.sleep(success_delay)
            return out
        except retry_exceptions as e:
            last_exc = e
            sleep = min(max_sleep, base_sleep * (2 ** attempt))
            _sleep_with_jitter(sleep)

    raise last_exc  # type: ignore[misc]


# -----------------------------
# Exception presets (optionnel mais pratique)
# -----------------------------
OPENAI_RETRY_EXCEPTIONS = (Exception,)
GEMINI_RETRY_EXCEPTIONS = (Exception,)
MISTRAL_RETRY_EXCEPTIONS = (Exception,) 
