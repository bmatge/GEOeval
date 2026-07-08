# evaluate.py
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Optional, Tuple, List

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, aliased

from models import Model, Test, RunResult, RunEvaluation, EvaluationPrompt

import llm_clients
from run import resolve_model
import time
import logging
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JudgeRunConfig:
    judge_model_id: int
    n_runs: int


# -----------------------------
# Normalisation des juges
# -----------------------------
# Un "juge" peut être fourni de plusieurs façons (todo.md) :
#   - JudgeRunConfig(judge_model_id=2, n_runs=2)          (rétro-compatibilité)
#   - {"model_id": 2, "repeats": 2}
#   - {"model": "gpt-5.2", "repeats": 2}                  (résolution nom -> model_id)
# Alias acceptés : "repeats" ou "n_runs" pour le nombre de passages.
JudgeSpec = "JudgeRunConfig | dict[str, Any]"


def _normalize_judges(
    session: Session, judges: list[Any]
) -> list[tuple[Model, int]]:
    normalized: list[tuple[Model, int]] = []
    for spec in judges:
        if isinstance(spec, JudgeRunConfig):
            model = resolve_model(session, spec.judge_model_id)
            repeats = spec.n_runs
        elif isinstance(spec, dict):
            repeats = int(spec.get("repeats", spec.get("n_runs", 1)))
            if "model_id" in spec:
                model = resolve_model(session, int(spec["model_id"]))
            elif "model" in spec:
                model = resolve_model(session, str(spec["model"]))
            else:
                raise ValueError(
                    f"Spec juge invalide {spec!r}: clé 'model' ou 'model_id' requise"
                )
        else:
            raise TypeError(f"Spec juge non supportée: {spec!r}")

        if repeats < 1:
            raise ValueError(f"repeats/n_runs doit être >= 1 (reçu {repeats})")
        normalized.append((model, repeats))
    return normalized


# -----------------------------
# Judge parsing
# -----------------------------
@dataclass(frozen=True)
class JudgeResult:
    label: str
    score: Decimal

    @staticmethod
    def from_json_obj(obj: Any) -> "JudgeResult":
        if not isinstance(obj, dict):
            raise ValueError("JSON must be an object")

        label = obj.get("label")
        score = obj.get("score")

        if not isinstance(label, str) or not label.strip():
            raise ValueError("label must be a non-empty string")

        try:
            score_dec = Decimal(str(score))
        except Exception as e:
            raise ValueError("score must be numeric") from e

        if score_dec < 0 or score_dec > 10:
            raise ValueError("score must be between 0 and 10")

        score_dec = score_dec.quantize(Decimal("0.01"))
        return JudgeResult(label=label.strip(), score=score_dec)


def build_prompt_json_guardrails(base_prompt: str) -> str:
    return (
        f"{base_prompt.strip()}\n\n"
        "IMPORTANT — FORMAT DE SORTIE OBLIGATOIRE:\n"
        "Réponds UNIQUEMENT avec un objet JSON valide (UTF-8) SANS markdown, SANS backticks, SANS commentaire.\n"
        'Schéma EXACT:\n'
        '{\n'
        '  "label": "texte court expliquant l’évaluation",\n'
        '  "score": 0-10\n'
        '}\n'
        "Aucune autre clé. Aucun autre texte.\n"
    )


def parse_judge_output(raw: str) -> JudgeResult:
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("empty judge output")

    try:
        obj = json.loads(raw)
        return JudgeResult.from_json_obj(obj)
    except Exception:
        pass

    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = raw[start : end + 1]
        obj = json.loads(candidate)
        return JudgeResult.from_json_obj(obj)

    raise ValueError("judge output is not valid JSON")


# -----------------------------
# LLM judge call
# -----------------------------
def call_judge_llm(judge_model: Model, user_prompt: str, organization_id: Optional[int] = None) -> str:
    logger.info("call_judge_llm START %s", judge_model.model_version)
    start = time.perf_counter()
    system_text = (
        "Tu es un évaluateur (LLM-as-a-judge). "
        "Tu dois suivre STRICTEMENT les instructions et retourner UNIQUEMENT du JSON valide, sans texte autour."
    )

    model_name = (judge_model.model_name or "").lower()

    # OpenAI (sans web)
    if model_name in {"openai", "chatgpt", "gpt"}:
        client = llm_clients.client_for_model(judge_model, organization_id=organization_id)

        def _do() -> str:
            resp = client.responses.create(
                model=judge_model.model_version,
                input=[
                    {"role": "system", "content": system_text},
                    {"role": "user", "content": user_prompt},
                ],
            )
            return resp.output_text or ""

        response = llm_clients.call_with_retry(
            _do,
            retry_exceptions=llm_clients.OPENAI_RETRY_EXCEPTIONS,
            max_retries=8,
            base_sleep=1.0,
            max_sleep=30.0,
            success_delay=0.2,
        )
        end = time.perf_counter()
        logger.info("call_judge_llm END (%.2f s)", end - start)
        return response

    # Mistral (sans agents, sans web)
    if model_name in {"mistral", "mistralai"}:
        client = llm_clients.client_for_model(judge_model, organization_id=organization_id)

        def _do() -> str:
            resp = client.chat.complete(
                model=judge_model.model_version,
                messages=[
                    {"role": "system", "content": system_text},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                top_p=0.95,
            )
            return resp.choices[0].message.content or ""

        response= llm_clients.call_with_retry(
            _do,
            retry_exceptions=llm_clients.MISTRAL_RETRY_EXCEPTIONS,
            max_retries=8,
            base_sleep=1.0,
            max_sleep=30.0,
            success_delay=0.2,
        )
        end = time.perf_counter()
        logger.info("call_judge_llm END (%.2f s)", end - start)
        return response 

    # Gemini (sans tools)
    if model_name in {"gemini", "google"}:
        client = llm_clients.client_for_model(judge_model, organization_id=organization_id)

        def _do() -> str:
            resp = client.models.generate_content(
                model=judge_model.model_version,
                contents=[
                    {"role": "user", "parts": [{"text": f"{system_text}\n\n{user_prompt}"}]},
                ],
                config=__import__("google.genai").genai.types.GenerateContentConfig(
                    temperature=0.3,
                    top_p=1,
                ),
            )
            return (resp.text or "")

        response= llm_clients.call_with_retry(
            _do,
            retry_exceptions=llm_clients.GEMINI_RETRY_EXCEPTIONS,
            max_retries=10,
            base_sleep=1.0,
            max_sleep=60.0,
            success_delay=0.2,
        )
        end = time.perf_counter()
        logger.info("call_judge_llm END (%.2f s)", end - start)
        return response

    # Albert (API souveraine Etalab) et tout endpoint compatible OpenAI
    # (chat completions, sans web)
    if model_name in {"albert", "etalab", "openai-compatible", "compatible-openai"}:
        client = llm_clients.client_for_model(judge_model, organization_id=organization_id)

        def _do() -> str:
            resp = client.chat.completions.create(
                model=judge_model.model_version,
                messages=[
                    {"role": "system", "content": system_text},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                top_p=0.95,
            )
            return resp.choices[0].message.content or ""

        response = llm_clients.call_with_retry(
            _do,
            retry_exceptions=llm_clients.ALBERT_RETRY_EXCEPTIONS,
            max_retries=8,
            base_sleep=1.0,
            max_sleep=30.0,
            success_delay=0.2,
        )
        end = time.perf_counter()
        logger.info("call_judge_llm END (%.2f s)", end - start)
        return response

    raise ValueError(f"Provider inconnu model_name={judge_model.model_name!r}")

def _record_judge_usage(session, org_id, model_id, run_id, prompt, resp):
    """Enregistre l'usage d'un appel juge (heuristique tokens). Best-effort."""
    if org_id is None:
        return
    try:
        from webapp import credentials, usage
        cred = credentials.get_for_model(session, org_id, model_id)
        billed = "byok" if (cred and cred.is_active and cred.api_key_encrypted) else "platform"
        usage.record(
            session,
            org_id=org_id, model_id=model_id, run_id=run_id,
            kind="judge", billed_to=billed,
            input_tokens=max(1, len(prompt or "") // 4),
            output_tokens=max(1, len(resp or "") // 4),
        )
    except Exception:  # noqa: BLE001
        logger.exception("usage judge non enregistré (run=%s model=%s)", run_id, model_id)


def evaluate_run(
    session: Session,
    run_id: int,
    judges: list[Any],
    organization_id: Optional[int] = None,
    progress_cb: Callable[[int, int, str], None] | None = None,
) -> None:
    """
    judges: liste de juges, chacun étant soit un JudgeRunConfig, soit un dict.
    Exemples équivalents:
        judges=[JudgeRunConfig(judge_model_id=5, n_runs=3)]
        judges=[{"model_id": 5, "repeats": 3}]
        judges=[{"model": "gemini-2.5-pro", "repeats": 3},
                {"model": "gpt-5.2", "repeats": 1}]
    Le nom de modèle est résolu en model_id via la table `models`.

    progress_cb: callback optionnel (current, total, detail) appelé après chaque
                 couple (juge, test) évalué (utilisé par l'UI).
    """
    judge_specs = _normalize_judges(session, judges)

    ResponsePrompt = aliased(EvaluationPrompt)
    CitationPrompt = aliased(EvaluationPrompt)

    stmt = (
        select(RunResult, Test, ResponsePrompt.prompt_text, CitationPrompt.prompt_text)
        .join(Test, Test.test_id == RunResult.test_id)
        .join(ResponsePrompt, ResponsePrompt.prompt_id == Test.response_quality_prompt_id)
        .join(CitationPrompt, CitationPrompt.prompt_id == Test.citation_quality_prompt_id)
        .where(RunResult.run_id == run_id)
    )
    rows = session.execute(stmt).all()
    if not rows:
        raise ValueError(f"Aucun run_results pour run_id={run_id}")

    n_evaluable = sum(1 for _, test, _, _ in rows if test.expected_answer)
    total = sum(repeats for _, repeats in judge_specs) * n_evaluable
    done = 0

    # Double boucle : juge (modèle, repeats) × index de run
    for judge_model, repeats in judge_specs:

        for judge_run_index in range(1, repeats + 1):

            for run_result, test, response_prompt_text, citation_prompt_text in rows:
                if not test.expected_answer:
                    continue

                if not response_prompt_text or not citation_prompt_text:
                    raise ValueError(
                        f"Test {test.test_id}: prompts manquants ou invalides"
                    )

                # 1) Qualité réponse
                response_quality_prompt = build_prompt_json_guardrails(response_prompt_text)
                response_quality_user_prompt = (
                    f"{response_quality_prompt}\n\n"
                    "=== DONNÉES À ÉVALUER ===\n"
                    f"[Réponse attendue]\n{test.expected_answer}\n\n"
                    f"[Réponse du modèle testé]\n{run_result.raw_answer}\n"
                    "Instruction: le champ [Réponse attendue] peut contenir plusieurs variantes "
                    "séparées par le token ' OU '. "
                    "Évaluer chaque variante indépendamment et conserver la meilleure note.\n"
                )

                response_quality_raw = call_judge_llm(
                    judge_model, response_quality_user_prompt, organization_id=organization_id
                )
                response_quality = parse_judge_output(response_quality_raw)
                _record_judge_usage(
                    session, organization_id, judge_model.model_id, run_id,
                    response_quality_user_prompt, response_quality_raw,
                )

                # 2) Qualité citation
                citation_quality_prompt = build_prompt_json_guardrails(citation_prompt_text)
                citation_quality_user_prompt = (
                    f"{citation_quality_prompt}\n\n"
                    "=== DONNÉES À ÉVALUER ===\n"
                    f"[Réponse du modèle testé]\n{run_result.raw_answer}\n"
                )

                citation_quality_raw = call_judge_llm(
                    judge_model, citation_quality_user_prompt, organization_id=organization_id
                )
                citation_quality = parse_judge_output(citation_quality_raw)
                _record_judge_usage(
                    session, organization_id, judge_model.model_id, run_id,
                    citation_quality_user_prompt, citation_quality_raw,
                )

                payload = dict(
                    run_id=run_id,
                    test_id=test.test_id,
                    judge_model_id=judge_model.model_id,
                    judge_run_index=judge_run_index,
                    response_quality_label=response_quality.label,
                    response_quality_score=response_quality.score,
                    citation_quality_label=citation_quality.label,
                    citation_quality_score=citation_quality.score,
                )

                ins = insert(RunEvaluation).values(**payload)
                upsert = ins.on_conflict_do_update(
                    index_elements=[
                        RunEvaluation.run_id,
                        RunEvaluation.test_id,
                        RunEvaluation.judge_model_id,
                        RunEvaluation.judge_run_index,
                    ],
                    set_={
                        "response_quality_label": ins.excluded.response_quality_label,
                        "response_quality_score": ins.excluded.response_quality_score,
                        "citation_quality_label": ins.excluded.citation_quality_label,
                        "citation_quality_score": ins.excluded.citation_quality_score,
                    },
                )

                session.execute(upsert)

                done += 1
                if progress_cb is not None:
                    progress_cb(
                        done,
                        total,
                        f"juge {judge_model.model_version} #{judge_run_index} · test {test.test_id}",
                    )


