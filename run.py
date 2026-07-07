from __future__ import annotations

import re
from datetime import date
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Test, Model, RunRow, RunResult

import llm_clients 
from google.genai import types
import time
import logging
logger = logging.getLogger(__name__)


_URL_RE = re.compile(r"https?://[^\s)>\"]+")


# -----------------------------
# DB helpers
# -----------------------------
def get_model(session: Session, model_id: int) -> Model:
    stmt = select(Model).where(Model.model_id == model_id)
    model = session.execute(stmt).scalars().first()
    if model is None:
        raise ValueError(f"model_id={model_id} introuvable dans models")
    return model


def get_model_by_version(session: Session, model_version: str) -> Model:
    stmt = select(Model).where(Model.model_version == model_version)
    models = session.execute(stmt).scalars().all()
    if not models:
        raise ValueError(f"model_version={model_version!r} introuvable dans models")
    if len(models) > 1:
        ids = ", ".join(str(m.model_id) for m in models)
        raise ValueError(
            f"model_version={model_version!r} ambigu (plusieurs model_id: {ids})"
        )
    return models[0]


def resolve_model(session: Session, ref: int | str) -> Model:
    """
    Résout un modèle depuis un model_id (int) OU un model_version (str, ex. 'gpt-5.2').
    """
    if isinstance(ref, bool):  # bool est un int en Python : à exclure explicitement
        raise TypeError("ref modèle invalide (bool)")
    if isinstance(ref, int):
        return get_model(session, ref)
    if isinstance(ref, str):
        return get_model_by_version(session, ref)
    raise TypeError(f"ref modèle invalide: {ref!r} (attendu int model_id ou str model_version)")


# -----------------------------
# Text helpers
# -----------------------------
def extract_urls(text: str) -> list[str]:
    urls = _URL_RE.findall(text or "")
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def build_instructions() -> str:
    today = date.today().isoformat()
    return (
        "Tu es un assistant conversationnel généraliste.\n"
        "Règles obligatoires :\n"
        "Donner des valeurs numériques précises lorsque possible (éviter les arrondis grossiers).\n"
        "Privilégier la précision numérique plutôt que la lisibilité simplifiée.\n"
        "Éviter les arrondis grossiers (ex: 10 % au lieu de 11,3 %).\n"
        "Réponds de façon utile, naturelle, claire, avec des paragraphes lisibles.\n"
        f"Nous sommes le {today} (fuseau Europe/Paris).\n"
        "Quand la question est ambiguë ou incomplète, privilégie une réponse directe plutôt qu’une question de clarification.\n"
        "Choisis l’interprétation la plus standard pour un grand public, réponds, puis indique brièvement l’hypothèse retenue.\n"
        "Ne pose une question que si une réponse sans clarification serait très probablement incorrecte ou risquerait de tromper l’utilisateur.\n"
    )


# -----------------------------
# LLM call (tested model)
# -----------------------------
def call_tested_llm(model: Model, prompt: str) -> str:
    logger.info("call_tested_llm START %s", model.model_version)
    start = time.perf_counter()
    instructions = build_instructions()
    model_name = (model.model_name or "").lower()

    # 1) OpenAI (web_search activé)
    if model_name in {"openai", "chatgpt", "gpt"}:
        client = llm_clients.get_openai_client_singleton()

        def _do() -> str:
            resp = client.responses.create(
                model=model.model_version,
                instructions=instructions,
                input=prompt,
                temperature=0.8,
                top_p=1,
                tools=[{
                    "type": "web_search",
                    "user_location": {
                        "type": "approximate",
                        "country": "FR",
                        "city": "Paris",
                        "timezone": "Europe/Paris",
                    },
                }],
                tool_choice="auto",
                include=["web_search_call.action.sources"],
            )
            return resp.output_text or ""

        response=llm_clients.call_with_retry(
            _do,
            retry_exceptions=llm_clients.OPENAI_RETRY_EXCEPTIONS,
            max_retries=8,
            base_sleep=1.0,
            max_sleep=30.0,
            success_delay=0.2,
        )
        end = time.perf_counter()
        logger.info("call_tested_llm END (%.2f s)", end - start)
        return response 

    # 2) Mistral (Agents/Conversations + web_search) + agent singleton par model_version
    if model_name in {"mistral", "mistralai"}:
        client = llm_clients.get_mistral_client_singleton()

        def _do() -> str:
            agent = llm_clients.get_mistral_agent_singleton_by_model_version(
                client=client,
                model_version=model.model_version,
                instructions=instructions,
                tools=[{"type": "web_search"}],
                completion_args={"temperature": 0.8, "top_p": 1},
                name="GEOeval Websearch Agent",
                description="Websearch agent for benchmark runs",
            )

            resp = client.beta.conversations.start(
                agent_id=agent.id,
                inputs=[{"role": "user", "content": prompt}],
            )

            texts = []
            for out in getattr(resp, "outputs", []) or []:
                if getattr(out, "type", None) == "message.output":
                    if isinstance(getattr(out, "content", None), str) and out.content.strip():
                        texts.append(out.content)
                        break
                    for chunk in getattr(out, "content", []) or []:
                        if hasattr(chunk, "text") and chunk.text:
                            texts.append(chunk.text)
                        elif isinstance(chunk, dict) and chunk.get("type") == "text" and chunk.get("text"):
                            texts.append(chunk["text"])
                    break

            out_text = "\n".join(texts).strip()
            if not out_text:
                raise RuntimeError(f"Mistral: output VIDE (resp={resp})")
            return out_text

        response= llm_clients.call_with_retry(
            _do,
            retry_exceptions=llm_clients.MISTRAL_RETRY_EXCEPTIONS,
            max_retries=8,
            base_sleep=1.0,
            max_sleep=30.0,
            success_delay=0.2,
        )
        end = time.perf_counter()
        logger.info("call_tested_llm END (%.2f s)", end - start)
        return response 

    # 3) Gemini (GoogleSearch activé)
    if model_name in {"gemini", "google"}:
        client = llm_clients.get_gemini_client_singleton()

        def _do() -> str:
            resp = client.models.generate_content(
                model=model.model_version,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=instructions,
                    temperature=0.8,
                    top_p=1,
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                ),
            )
            out_text = (getattr(resp, "text", None) or "").strip()
            if not out_text:
                raise RuntimeError(f"Gemini: output VIDE (resp={resp})")
            return out_text

        response= llm_clients.call_with_retry(
            _do,
            retry_exceptions=llm_clients.GEMINI_RETRY_EXCEPTIONS,
            max_retries=10,
            base_sleep=1.0,
            max_sleep=60.0,
            success_delay=0.2,
        )
        end = time.perf_counter()
        logger.info("call_tested_llm END (%.2f s)", end - start)
        return response 

    raise ValueError(f"Provider inconnu model_name={model.model_name!r}")


def execute_run(
    session: Session,
    tested_model: int | str,
    tests: list[Test],
    run_meta: Optional[dict[str, Any]] = None,
) -> int:
    """
    Exécute un run et écrit runs + run_results.

    tested_model : model_id (int) OU model_version (str, ex. 'gpt-5.2').
    Retourne run_id.
    """
    tested_model = resolve_model(session, tested_model)

    # 1) Appels LLM -> mémoire
    results: list[tuple[int, str, Optional[list[str]]]] = []
    for t in tests:
        answer = call_tested_llm(tested_model, t.prompt)
        citations = extract_urls(answer)
        results.append((t.test_id, answer, citations if citations else None))

    # 2) Écriture DB
    run_row = RunRow(tested_model_id=tested_model.model_id, run_meta=run_meta)
    session.add(run_row)
    session.flush()
    run_id = run_row.run_id

    for test_id, raw_answer, raw_citations in results:
        session.add(
            RunResult(
                run_id=run_id,
                test_id=test_id,
                raw_answer=raw_answer,
                raw_citations=raw_citations,
            )
        )

    return run_id

