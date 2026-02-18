import time

from db import SessionLocal
from load import load_tests
from run import execute_run
from evaluate import evaluate_run,JudgeRunConfig
import logging
from logging.handlers import RotatingFileHandler

file_handler = RotatingFileHandler(
    "geoeval.log",
    maxBytes=5_000_000,  # 5 MB
    backupCount=3,
    encoding="utf-8"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[file_handler, logging.StreamHandler()]
)

logger = logging.getLogger(__name__)

if __name__ == "__main__":
    logger.info("début")
    start = time.perf_counter()
    
    #2 = gpt-5.2 / 3 = mistral-large-latest / 4 = gemini-pro-latest / 5 gemini-2.5-pro
    tested_models_id=[2,3,4]
    for tested_model_id in tested_models_id:
        logger.info("tested_model_id = %s", tested_model_id)
        # 1) Charger les tests + exécuter le run
        with SessionLocal() as session:
            try:
                logger.info("PHASE RUN")
                tests = load_tests(session, active_only=True, ready_only=True)
                run_id = execute_run(session,tested_model_id=tested_model_id,tests=tests,run_meta={"note": "baseline"},)
                session.commit()
            except:
                session.rollback()
                logger.exception("Error in PHASE RUN")
                raise

        # 2) Évaluer le run 
        with SessionLocal() as session:
            try:
                logger.info("PHASE EVALUATION")
                evaluate_run(session,run_id=run_id,judge_run_configs=[JudgeRunConfig(judge_model_id=5, n_runs=1)],)
                session.commit()
            except:
                session.rollback()
                logger.exception("Error in PHASE EVALUATION")
                raise

    end = time.perf_counter()
    logger.info("Durée totale d'exécution : %.2f secondes", end - start)
    logger.info("run_id = %s", run_id)



