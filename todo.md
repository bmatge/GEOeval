1- corriger signature de evaluate_run
=> evaluate_run(run_id, judges=[{"model_id": 2, "repeats": 2}])

2- 2e correction sur l'appel des modèles
=>  judges=[
        {"model": "gpt-5.2", "repeats": 2},
        {"model": "gpt-4.1-mini", "repeats": 1},
    ],
)
Et en interne tu convertis "gpt-5.2" → model_id via la table models