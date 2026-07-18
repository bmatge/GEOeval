import time

from db import SessionLocal
from load import load_tests
from run import execute_run
from evaluate import evaluate_run
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
    
    # Modèles testés et juge désignés par NOM (model_version), résolus en model_id
    # en interne via la table `models`.
    tested_models = ["gpt-5.2", "mistral-large-latest", "gemini-pro-latest"]
    judges = [{"model": "gemini-2.5-pro", "repeats": 1}]
    # CLI historique : rattache tout à l'org seed (slug « bertrand », id=1).
    ORG_ID = 1

    for tested_model in tested_models:
        logger.info("tested_model = %s", tested_model)
        # 1) Charger les tests + exécuter le run
        with SessionLocal() as session:
            try:
                logger.info("PHASE RUN")
                tests = load_tests(
                    session, active_only=True, ready_only=True, organization_id=ORG_ID
                )
                run_id = execute_run(
                    session,
                    tested_model=tested_model,
                    tests=tests,
                    organization_id=ORG_ID,
                    run_meta={"note": "baseline"},
                )
                session.commit()
            except:
                session.rollback()
                logger.exception("Error in PHASE RUN")
                raise

        # 2) Évaluer le run
        with SessionLocal() as session:
            try:
                logger.info("PHASE EVALUATION")
                evaluate_run(session, run_id=run_id, judges=judges)
                session.commit()
            except:
                session.rollback()
                logger.exception("Error in PHASE EVALUATION")
                raise

    end = time.perf_counter()
    logger.info("Durée totale d'exécution : %.2f secondes", end - start)
    logger.info("run_id = %s", run_id)



