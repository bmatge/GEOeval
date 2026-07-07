# evaluate.py
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Tuple, List

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, aliased

from models import Model, Test, RunResult, RunEvaluation, EvaluationPrompt

import llm_clients  
import time
import logging
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JudgeRunConfig:
    judge_model_id: int
    n_runs: int


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
def call_judge_llm(judge_model: Model, user_prompt: str) -> str:
    logger.info("call_judge_llm START %s", judge_model.model_version)
    start = time.perf_counter()
    system_text = (
        "Tu es un évaluateur (LLM-as-a-judge). "
        "Tu dois suivre STRICTEMENT les instructions et retourner UNIQUEMENT du JSON valide, sans texte autour."
    )

    model_name = (judge_model.model_name or "").lower()

    # OpenAI (sans web)
    if model_name in {"openai", "chatgpt", "gpt"}:
        client = llm_clients.get_openai_client_singleton()

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
        client = llm_clients.get_mistral_client_singleton()

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
        client = llm_clients.get_gemini_client_singleton()

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

    raise ValueError(f"Provider inconnu model_name={judge_model.model_name!r}")

def evaluate_run(session: Session,run_id: int,judge_run_configs: list[JudgeRunConfig],) -> None:
    """
    judge_run_configs: liste de configs (judge_model_id, n_runs)
    Exemple:
        [
            JudgeRunConfig(judge_model_id=5, n_runs=3),
            JudgeRunConfig(judge_model_id=4, n_runs=2),
        ]
    """

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

    # Préchargement des modèles juges
    judge_model_ids = sorted({cfg.judge_model_id for cfg in judge_run_configs})
    stmt_models = select(Model).where(Model.model_id.in_(judge_model_ids))
    judge_models_by_id = {
        m.model_id: m for m in session.execute(stmt_models).scalars().all()
    }

    missing = [mid for mid in judge_model_ids if mid not in judge_models_by_id]
    if missing:
        raise ValueError(f"judge_model_id inconnus dans models: {missing}")

    # Double boucle : config (modèle, n_runs) × index de run
    for cfg in judge_run_configs:
        judge_model = judge_models_by_id[cfg.judge_model_id]

        for judge_run_index in range(1, cfg.n_runs + 1):

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
                    judge_model, response_quality_user_prompt
                )
                response_quality = parse_judge_output(response_quality_raw)

                # 2) Qualité citation
                citation_quality_prompt = build_prompt_json_guardrails(citation_prompt_text)
                citation_quality_user_prompt = (
                    f"{citation_quality_prompt}\n\n"
                    "=== DONNÉES À ÉVALUER ===\n"
                    f"[Réponse du modèle testé]\n{run_result.raw_answer}\n"
                )

                citation_quality_raw = call_judge_llm(
                    judge_model, citation_quality_user_prompt
                )
                citation_quality = parse_judge_output(citation_quality_raw)

                payload = dict(
                    run_id=run_id,
                    test_id=test.test_id,
                    judge_model_id=cfg.judge_model_id,
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


