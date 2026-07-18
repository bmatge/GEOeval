# llm_clients.py
from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from decimal import Decimal
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
_ALBERT_CLIENT_SINGLETON: Optional[OpenAI] = None


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


def get_albert_client_singleton() -> "OpenAI":
    """Albert (API souveraine Etalab/DINUM), compatible OpenAI — client dédié."""
    global _ALBERT_CLIENT_SINGLETON
    if _ALBERT_CLIENT_SINGLETON is None:
        from openai import OpenAI
        _ALBERT_CLIENT_SINGLETON = OpenAI(
            api_key=os.environ["ALBERT_API_KEY"],
            # `or` (et non un défaut de .get) : compose peut poser la variable vide
            base_url=os.environ.get("ALBERT_BASE_URL") or "https://albert.api.etalab.gouv.fr/v1",
        )
    return _ALBERT_CLIENT_SINGLETON


# -----------------------------
# Clients paramétrés par modèle (page Modèles : base_url / api_key / en-têtes)
# -----------------------------
# Famille de provider -> pilote quel SDK/branche utiliser. Les champs du modèle
# priment ; vides => repli sur les variables d'environnement / défauts.
_FAMILY_BY_NAME = {
    "openai": "openai", "chatgpt": "openai", "gpt": "openai",
    "albert": "albert", "etalab": "albert",
    "openai-compatible": "generic", "compatible-openai": "generic",
    "mistral": "mistral", "mistralai": "mistral",
    "gemini": "gemini", "google": "gemini",
    "openrouter": "openrouter",
}

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# En-têtes d'attribution recommandés par OpenRouter (classement d'app, diagnostic).
_OPENROUTER_HEADERS = {
    "HTTP-Referer": "https://geoeval.lab.miweb.run",
    "X-Title": "GEOeval",
}

_CLIENTS_BY_CONFIG: dict[tuple, Any] = {}


def provider_family(model_name: Optional[str]) -> Optional[str]:
    return _FAMILY_BY_NAME.get((model_name or "").lower())


def _headers_key(headers: Optional[dict]) -> str:
    import json
    return json.dumps(headers, sort_keys=True) if headers else ""


def _byok_override(model: Any, organization_id: Optional[int]) -> tuple[Optional[str], Optional[str], Optional[dict]]:
    """Cherche une clé BYOK active pour (org, modèle). Renvoie (base_url, api_key, headers) ou (None, None, None)."""
    if organization_id is None:
        return (None, None, None)
    # Import local pour éviter un cycle au chargement.
    try:
        from db import SessionLocal
        from models import OrgCredential
        from webapp.crypto import decrypt_secret
    except Exception:  # noqa: BLE001
        return (None, None, None)

    from sqlalchemy import select
    with SessionLocal() as session:
        cred = session.execute(
            select(OrgCredential).where(
                OrgCredential.organization_id == organization_id,
                OrgCredential.model_id == model.model_id,
                OrgCredential.is_active.is_(True),
            )
        ).scalar_one_or_none()
        if cred is None:
            return (None, None, None)
        api_key = decrypt_secret(cred.api_key_encrypted)
        return (cred.base_url or None, api_key, cred.extra_headers or None)


def client_for_model(model: Any, organization_id: Optional[int] = None) -> Any:
    """Client LLM pour un modèle avec résolution en cascade des credentials.

    Ordre de résolution (ADR-078 §2) :
        org_credentials (BYOK, si organization_id fourni)
        → models.api_key / base_url / headers (config plateforme)
        → variables d'environnement du provider.
    Mis en cache par configuration effective.
    """
    family = provider_family(model.model_name)
    if family is None:
        raise ValueError(f"Provider inconnu model_name={model.model_name!r}")

    byok_base, byok_key, byok_headers = _byok_override(model, organization_id)
    base_url = byok_base or getattr(model, "base_url", None) or None
    api_key = byok_key or getattr(model, "api_key", None) or None
    headers = byok_headers or getattr(model, "extra_headers", None) or None
    key = (family, base_url, api_key, _headers_key(headers))
    if key in _CLIENTS_BY_CONFIG:
        return _CLIENTS_BY_CONFIG[key]

    if family == "openai":
        from openai import OpenAI
        client = OpenAI(
            api_key=api_key or os.environ["OPENAI_API_KEY"],
            base_url=base_url,
            default_headers=headers,
        )
    elif family == "albert":
        from openai import OpenAI
        client = OpenAI(
            api_key=api_key or os.environ["ALBERT_API_KEY"],
            base_url=base_url or os.environ.get("ALBERT_BASE_URL") or "https://albert.api.etalab.gouv.fr/v1",
            default_headers=headers,
        )
    elif family == "generic":
        # Endpoint compatible OpenAI arbitraire : URL et clé obligatoires en base.
        if not base_url:
            raise ValueError(
                f"Modèle {model.model_version!r} (compatible-openai) : base_url requis (page Modèles)."
            )
        from openai import OpenAI
        client = OpenAI(api_key=api_key or "none", base_url=base_url, default_headers=headers)
    elif family == "openrouter":
        # Provider plateforme par défaut (ADR-080) : compatible OpenAI, clé unique.
        from openai import OpenAI
        client = OpenAI(
            api_key=api_key or os.environ["OPENROUTER_API_KEY"],
            base_url=base_url or OPENROUTER_BASE_URL,
            default_headers={**_OPENROUTER_HEADERS, **(headers or {})},
        )
    elif family == "mistral":
        try:
            from mistralai import Mistral  # SDK v1.x
        except ImportError:
            from mistralai.client import Mistral  # SDK v2.x (namespace package)
        kwargs: dict[str, Any] = {"api_key": api_key or os.environ["MISTRAL_API_KEY"]}
        if base_url:
            kwargs["server_url"] = base_url
        client = Mistral(**kwargs)
    elif family == "gemini":
        from google import genai
        from google.genai import types as genai_types
        kwargs = {"api_key": api_key or os.environ["GEMINI_API_KEY"]}
        if base_url or headers:
            kwargs["http_options"] = genai_types.HttpOptions(
                base_url=base_url or None, headers=headers or None
            )
        client = genai.Client(**kwargs)
    else:  # pragma: no cover
        raise ValueError(f"famille inconnue: {family}")

    _CLIENTS_BY_CONFIG[key] = client
    return client


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
# Usage réel (ADR-080 §6.3)
# -----------------------------
@dataclass(frozen=True)
class LLMUsage:
    """Tokens et coût réels renvoyés par le provider (OpenRouter : usage.include).

    `cost_usd` est le total facturé par OpenRouter (web search inclus), en USD —
    la conversion EUR se fait à l'ingestion (webapp/usage.py).
    """
    input_tokens: int
    output_tokens: int
    cost_usd: Optional[Decimal] = None


def citations_from_openrouter_message(message: Any) -> list[str]:
    """URLs des annotations `url_citation` d'un message OpenRouter (ADR-080 §2.4).

    Format standardisé quel que soit le moteur (natif ou Exa) ; dédoublonné,
    ordre d'apparition conservé. Tolère objets SDK et dicts.
    """
    annotations = getattr(message, "annotations", None) or []
    urls: list[str] = []
    for a in annotations:
        if isinstance(a, dict):
            kind = a.get("type")
            citation = a.get("url_citation") or {}
            url = citation.get("url")
        else:
            kind = getattr(a, "type", None)
            citation = getattr(a, "url_citation", None)
            url = getattr(citation, "url", None) if citation is not None else None
        if kind == "url_citation" and url and url not in urls:
            urls.append(url)
    return urls


def openrouter_web_extra_body(search_config: Optional[dict]) -> dict:
    """extra_body OpenRouter (plugin web + usage réel) depuis models.search_config.

    Config vide ou engine="off" → pas de plugin (aucune recherche web).
    `allowed_domains` (nom ADR-080) est mappé sur `include_domains` (nom API).
    """
    extra_body: dict[str, Any] = {"usage": {"include": True}}
    sc = search_config or {}
    engine = (sc.get("engine") or "off").lower()
    if engine == "off":
        return extra_body
    plugin: dict[str, Any] = {"id": "web", "engine": engine}
    if sc.get("max_results"):
        plugin["max_results"] = int(sc["max_results"])
    if sc.get("allowed_domains"):
        plugin["include_domains"] = list(sc["allowed_domains"])
    extra_body["plugins"] = [plugin]
    if sc.get("search_context_size"):
        extra_body["web_search_options"] = {"search_context_size": sc["search_context_size"]}
    return extra_body


def usage_from_openrouter_response(resp: Any) -> Optional[LLMUsage]:
    """Extrait un LLMUsage de la réponse chat.completions d'OpenRouter (best-effort)."""
    u = getattr(resp, "usage", None)
    if u is None:
        return None
    cost = getattr(u, "cost", None)
    if cost is None and getattr(u, "model_extra", None):
        cost = u.model_extra.get("cost")
    return LLMUsage(
        input_tokens=int(getattr(u, "prompt_tokens", 0) or 0),
        output_tokens=int(getattr(u, "completion_tokens", 0) or 0),
        cost_usd=Decimal(str(cost)) if cost is not None else None,
    )


# -----------------------------
# Retry / throttle helpers
# -----------------------------
def _sleep_with_jitter(seconds: float) -> None:
    # jitter 70%-130%
    seconds = seconds * (0.7 + 0.6 * random.random())
    time.sleep(seconds)


class LLMCallError(RuntimeError):
    """Erreur LLM non transitoire (quota dur, clé invalide, requête refusée) :
    remontée immédiatement, sans retry."""


# Codes HTTP dont le retry ne peut rien changer (même requête → même refus).
_NON_RETRYABLE_STATUS = {400, 401, 403, 404, 422}
# Marqueurs d'un 429 "quota dur" (plan/facturation), par opposition à un simple
# rate limit par minute qui, lui, se débloque en réessayant.
_HARD_QUOTA_MARKERS = ("check your plan and billing", "limit: 0")


def _non_retryable_reason(exc: BaseException) -> Optional[str]:
    """Renvoie la raison si l'erreur est non transitoire, None sinon.

    Duck-typing sur le code HTTP : openai/mistralai exposent `.status_code`,
    google-genai expose `.code`.
    """
    code = getattr(exc, "status_code", None)
    if not isinstance(code, int):
        code = getattr(exc, "code", None)
    if not isinstance(code, int):
        return None
    if code in _NON_RETRYABLE_STATUS:
        return f"HTTP {code} (clé invalide, accès refusé ou requête rejetée)"
    if code == 429:
        msg = str(exc).lower()
        if any(marker in msg for marker in _HARD_QUOTA_MARKERS):
            return "HTTP 429 quota épuisé côté provider (plan/facturation)"
    return None


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
    Les erreurs non transitoires (voir _non_retryable_reason) sont remontées
    immédiatement en LLMCallError, sans épuiser les retries.
    """
    last_exc: Optional[BaseException] = None

    for attempt in range(max_retries):
        try:
            out = fn()
            if success_delay > 0:
                time.sleep(success_delay)
            return out
        except retry_exceptions as e:
            reason = _non_retryable_reason(e)
            if reason is not None:
                detail = str(e)
                if len(detail) > 300:
                    detail = detail[:300] + "…"
                raise LLMCallError(f"{reason} — inutile de réessayer. Détail : {detail}") from e
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
ALBERT_RETRY_EXCEPTIONS = (Exception,)
OPENROUTER_RETRY_EXCEPTIONS = (Exception,)
