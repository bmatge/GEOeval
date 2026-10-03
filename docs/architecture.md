# Architecture logique GEOeval — POC actuel et cible pilote

Document de référence lié à [ADR-088](adr/ADR-088-stack-conservee-refacto-incremental-nubo.md)
et [ADR-089](adr/ADR-089-modele-cible-echelle-ministerielle.md) (modèle de données cible pour l'échelle).
Les diagrammes sont en Mermaid (rendus par GitHub). Trois vues : l'existant, la cible, le
déroulé d'un run. Puis deux tableaux : où vivent les règles métier, et comment on atteint
chaque fournisseur LLM.

## 1. Existant (POC sur VibeLab, avant le lot 1.2)

Un seul processus uvicorn porte tout : les routes HTML, le worker de runs (thread) et le
planificateur (thread). Les règles métier sont réparties entre la couche services et les
contrôleurs de `geoeval/web/app.py`.

```mermaid
flowchart LR
  subgraph Clients
    U["Navigateur<br/>UI DSFR"]
  end

  subgraph VibeLab["VibeLab — VPS spawn"]
    GATE["Traefik + gate magic-link<br/>(AUTH=link)"]
    subgraph WEB["Conteneur geoeval-web — 1 processus uvicorn"]
      MW["AuthMiddleware<br/>session cookie · headers proxy · DEV_FAKE_EMAIL"]
      APP["geoeval/web/app.py<br/>83 routes : 77 HTML + 6 JSON<br/>+ règles budget et allowlist dans les contrôleurs"]
      SVC["services · tenancy · budget · pricing<br/>org_models · credentials · audit"]
      JOBS["JobManager<br/>thread unique · file et logs en mémoire"]
      SCHED["Scheduler<br/>thread · poll 30 s"]
    end
    DB[("PostgreSQL 16<br/>23 tables")]
  end

  subgraph Core["Cœur benchmark — modules racine"]
    RUN["run.py<br/>execute_run"]
    EVAL["evaluate.py<br/>evaluate_run · JSON strict"]
    LLMC["llm_clients.client_for_model<br/>cascade contrat d'entité (hérité) → config modèle → .env<br/>retry · fail-fast"]
  end

  subgraph LLM["Fournisseurs LLM — egress Internet libre"]
    OR["OpenRouter<br/>chat.completions + plugin web (natif/Exa)"]
    OAI["OpenAI<br/>Responses + web_search"]
    MIS["Mistral<br/>Agents + web_search"]
    GEM["Gemini<br/>GoogleSearch"]
    ALB["Albert / OpenAI-compatible<br/>juge souverain, sans web"]
  end
  IDP["Authentik<br/>OIDC optionnel"]

  U --> GATE --> MW --> APP --> SVC --> DB
  APP -->|"lancer (check_budget, allowlist)"| JOBS
  SCHED -->|"scheduled_runs échues"| JOBS
  SCHED --> DB
  JOBS --> RUN --> EVAL
  RUN --> LLMC
  EVAL --> LLMC
  LLMC --> OR
  LLMC --> OAI
  LLMC --> MIS
  LLMC --> GEM
  LLMC --> ALB
  RUN -->|"run_results · usage"| DB
  EVAL -->|"run_evaluations · usage"| DB
  MW -.-> IDP
```

Limites visibles sur ce schéma (levées par le lot 1.2 : table `jobs`, processus
`geoeval.worker.main`, verrou consultatif pour le planificateur) : un redémarrage perdait la
file et les logs de jobs ; deux réplicas auraient doublé le planificateur ; les contrôles budget et allowlist ne protègent que
le chemin HTML ; l'egress n'est pas filtré.

## 2. Cible pilote (lot 1 + API first + lot 2 Nubo)

Même image Docker, trois rôles de déploiement : **web**, **worker**, **migration**. L'API
v1 et l'UI consomment la même couche services, qui porte toutes les règles. Le worker lit
une table `jobs` en base avec un verrou PostgreSQL. Les secrets, les logs et les sondes
sont ceux de la plateforme.

```mermaid
flowchart LR
  subgraph Clients
    U["Navigateur<br/>UI DSFR"]
    M["Clients machine<br/>Jenkins · cron · Grist · export"]
  end

  subgraph Nubo["Nubo (cible) — même image Docker que VibeLab"]
    ING["Ingress / reverse proxy"]
    subgraph WEB["Déploiement web — N réplicas"]
      AUTH["Authentification<br/>UI : session ProConnect (OIDC)<br/>API : jeton porteur d'organisation"]
      API["geoeval/web/api/v1<br/>JSON · Pydantic · OpenAPI · pagination · erreurs normalisées"]
      UI["geoeval/web/ui<br/>routers HTML DSFR minces"]
      SVC["Couche services = toutes les règles<br/>RBAC · budget · allowlist modèles · ADR-076 · audit"]
      HZ["/healthz · /readyz · /metrics<br/>(lot 1.5 : livré, web et worker :9100)"]
    end
    subgraph WK["Déploiement worker — N réplicas"]
      W["worker.py<br/>claim : SELECT … FOR UPDATE SKIP LOCKED<br/>arrêt gracieux SIGTERM · reprise"]
      SCH["Scheduler<br/>verrou PostgreSQL (advisory lock)"]
      RUN["run.py"]
      EVAL["evaluate.py"]
      LLMC["llm_clients<br/>cascade contrat d'entité → modèle → secret plateforme<br/>via proxy de sortie"]
    end
    MIG["Service migrate (one-shot)<br/>alembic upgrade head + seed<br/>(lot 1.4 : livré, révision 0001 convergente)"]
    DB[("PostgreSQL managé<br/>23 tables + jobs · job_logs")]
    SEC["Secrets plateforme<br/>clés LLM · Fernet (contrats) · session"]
    LOGS["Logs JSON stdout → collecte plateforme<br/>(lot 1.5 : request_id, job_id, org_id)"]
  end

  subgraph EXT["Flux sortants — allowlist egress à obtenir"]
    OR["OpenRouter<br/>modèles testés + juges non souverains"]
    ALB["Albert<br/>juge souverain, en direct"]
    LEG["OpenAI · Mistral · Gemini en direct<br/>(modèles legacy désactivés, historique conservé)"]
    PC["ProConnect"]
    NEX["Nexus<br/>images de base · index pip"]
  end

  U --> ING
  M --> ING
  ING --> AUTH
  AUTH --> API
  AUTH --> UI
  API --> SVC
  UI --> SVC
  SVC --> DB
  SVC -->|"INSERT jobs (queued)"| DB
  W -->|"claim → running → done"| DB
  SCH --> DB
  W --> RUN --> EVAL
  RUN --> LLMC
  EVAL --> LLMC
  LLMC --> OR
  LLMC --> ALB
  LLMC -.-> LEG
  RUN -->|"run_results · usage · job_logs"| DB
  EVAL -->|"run_evaluations · usage"| DB
  MIG --> DB
  AUTH -.-> PC
  SEC -.-> WEB
  SEC -.-> WK
  WEB --> LOGS
  WK --> LOGS
  NEX -.->|"build CI/CD"| Nubo
```

Principe API first : toute fonctionnalité existe d'abord dans `geoeval/web/api/v1`
(livré : 1.3b lecture, lancement, jobs, jetons ; 1.3c écriture du corpus et des planifications). L'UI ne
peut rien faire que l'API ne permette pas. L'UI n'appelle pas l'API en HTTP : les deux
partagent la couche services, qui est le seul endroit où vivent les règles.

## 3. Déroulé d'un run (cible)

```mermaid
sequenceDiagram
  autonumber
  participant C as Client (UI ou API v1)
  participant S as Services (règles)
  participant DB as PostgreSQL
  participant W as Worker
  participant L as llm_clients
  participant T as Modèle testé (OpenRouter)
  participant J as Juge (Albert / OpenRouter)

  C->>S: lancer run (périmètre, modèles testés, juges × repeats)
  S->>S: RBAC editor+ · allowlist modèles de l'org · estimation coût
  S->>DB: check_budget (plafond mois + plafond jour)
  alt budget dépassé
    S-->>C: refus explicite (motif, reste disponible)
  else budget OK
    S->>DB: INSERT jobs (queued) + audit_log
    S-->>C: 202 job_id
  end

  W->>DB: claim job (FOR UPDATE SKIP LOCKED) → running
  loop pour chaque modèle testé
    W->>DB: load_tests(org, périmètre, actifs, prêts)
    W->>L: client_for_model(modèle, org)
    L->>DB: contrat de l'entité ou d'un ancêtre ? (clé déchiffrée Fernet)
    Note over L: expiré / au plafond : blocage ; sans contrat : config du modèle, puis secret plateforme
    loop pour chaque test
      W->>T: question + recherche web (search_config : natif / Exa / off)
      T-->>W: réponse · citations url_citation · usage réel
      W->>DB: run_results + usage (cost_usd → cost_eur)
    end
    loop pour chaque juge × repeats
      W->>J: prompt + garde-fous JSON (réponse attendue, citations)
      J-->>W: {"label", "score"} — parse strict, 0–10
      W->>DB: run_evaluations + usage
    end
  end
  W->>DB: job done + job_logs
  C->>S: GET /api/v1/jobs/{id} · /api/v1/runs/{id}
```

Les erreurs LLM non transitoires (401, 403, 404, 422, 429 quota dur) remontent
immédiatement en `LLMCallError` ; les autres sont réessayées avec backoff et jitter.

## 4. Règles métier : où elles vivent

| Règle | Source | Aujourd'hui | Cible API first |
|---|---|---|---|
| RBAC trois rôles + admin plateforme, hérités vers le bas | ADR-077, ADR-089 §2.4 | `tenancy.resolve_role` (E2) appelé par `geoeval/web/deps.py` et `geoeval/web/api/deps.py` ; jetons hérités | Idem |
| Délégation de la structure de l'arbre | ADR-089 §2.4 | `tenancy.can_create_under` / `can_qualify` / `can_restructure` (E2) | Idem |
| Lecture publique des tableaux de bord | ADR-087 | `deps.public_org` | Idem, endpoints API en lecture sans jeton |
| Plafond budget mois et jour, consolidé dans l'arbre | ADR-080, ADR-089 §2.3 | `geoeval/web/budget.py` appelé par `launching` (lancement, « exécuter maintenant ») et par le planificateur à l'échéance (E3) | Idem |
| Alertes 80 / 100 % (app + email) | ADR-089 §2.8 | `geoeval/web/budget_alerts.py` (E3) → `notifications.notify` (E7), en fin de job et après un saut | Idem |
| Notifications par rôle, préférences d'email, idempotence | ADR-089 §2.8 | `geoeval/web/notifications.py` (E7) | Webhooks, récapitulatifs |
| Détecteurs : évaluation en échec, contrats expirants, question toujours fausse | ADR-089 §2.8 | `geoeval/web/detectors.py` (E7), appelés par le worker (`jobs.execute`) et le planificateur | Chute des citations officielles |
| Liste blanche des modèles par org | EPIC-001, ADR-089 | `geoeval/web/launching.py` + `org_models` : intersection sur la chaîne d'entités via `hierarchy.resolve_restrictive` (E1) | Idem |
| Paramètres hérités dans l'arbre d'entités | ADR-089 §2.2 | `geoeval/web/hierarchy.py` (E1) : `resolve_nearest`, `resolve_restrictive` | Budget (E3), contrats (E5), notifications (E7) |
| Questions effectives d'un périmètre (propres + pools abonnés visibles) | ADR-089 §2.5 | `launching.tests_for_run` → `pools.effective_tests` (E4), pour la validation, le devis, le planificateur et le worker | Idem |
| Visibilité des pools, inclusions sans cycle, composition sans élargissement | ADR-089 §2.5 | `geoeval/web/pools.py` (E4) | Idem |
| Catalogue de thèmes réservé à l'admin plateforme | ADR-089 §2.5 | `geoeval/web/themes.py` + `deps.require_platform_admin` (E4) | Idem |
| Cycle de vie des questions (brouillon / publiée / retirée) | ADR-089 §2.9 | `services.publish_test` / `deactivate_test` ; filtre « publiée » dans `load_tests` et `pools.effective_tests` (E8) | Validation métier |
| Campagnes : protocole figé, participants du sous-arbre, grilles verrouillées | ADR-089 §2.9 | `geoeval/web/campaigns.py` (E8), exécutées par le planificateur via `launching.estimate_and_check_budget(campaign_id=…)` | Rejugement versionné |
| Échéances des planifications, réactivation d'un one-shot passé | — | `geoeval/web/scheduling.py` (lot 1.3c) | Idem |
| Historique inviolable : désactivation, jamais suppression | ADR-076 | `services.delete_model` refuse si runs référencés ; tests désactivés | Inchangé, exposé tel quel dans l'API (pas de DELETE sur runs) |
| Cascade des clés : contrat d'entité (hérité) → modèle → plateforme ; contrat expiré ou épuisé = blocage | ADR-078, ADR-089 §2.6 | `contracts.resolve` appelé par `llm_clients._contract_override` (E5) ; imputation `usage.contract_id` | Secret plateforme fourni par Nubo |
| Politique de routage restrictive héritée (fournisseurs autorisés ; notateurs souverains / UE) | ADR-089 §2.6 | `geoeval/web/routing.py` (E5), vérifiée par `launching.check_compliance` (lancement, planification, échéance) et appliquée aux formulaires | Idem |
| Plafond d'un contrat sur sa durée | ADR-089 §2.6 | `launching._check_contract_caps` (devis imputé) + blocage à l'appel (E5) | Idem |
| Retry, fail-fast, quota dur | — | `llm_clients.call_with_retry` | Inchangé |
| Sortie JSON stricte des juges, score 0–10 | ADR-079 | `evaluate.parse_judge_output` | Inchangé |
| Coût réel USD → EUR à l'ingestion | ADR-080 §6.3 | `geoeval/web/usage.record` | Inchangé |
| Journal d'audit | ADR-077 | `geoeval/web/audit.record` depuis les contrôleurs | Appelé depuis les services |
| Sérialisation des runs (quotas API) | — | Thread unique | Concurrence bornée par fournisseur dans le worker |

Lot 1.3a : le contrôle budget et la liste blanche ont quitté les contrôleurs HTML pour le
service `launching`, que l'UI et l'API v1 appelleront à l'identique. Depuis E3, le planificateur revérifie le budget consolidé à l'échéance : une exécution qui
dépasserait un plafond est sautée, tracée sur la planification et dans l'audit, et une
alerte est émise.

## 5. Routes vers les fournisseurs LLM

| Famille (`models.model_name`) | SDK / API | Recherche web | Rôle | Cible |
|---|---|---|---|---|
| `openrouter` | `openai` (base_url OpenRouter), `chat.completions` + `plugins: web` | natif ou Exa, par modèle (`search_config`) | testé et juge | **défaut pour tous les modèles testés** (ADR-080) |
| `albert` / `openai-compatible` | `openai` (base_url Albert), `chat.completions` | aucune | juge souverain | conservé en direct |
| `openai` / `chatgpt` / `gpt` | `openai`, API Responses + `web_search` | native, geo FR-Paris | testé, juge | legacy, désactivé après bascule |
| `mistral` | `mistralai`, API Agents + `web_search` | native | testé, juge | legacy |
| `gemini` / `google` | `google-genai`, `GoogleSearch` | native | testé, juge | legacy |

Chaque famille résout sa clé par la même cascade (E5) : contrat actif le plus proche dans
l'arbre de l'entité (un contrat expiré ou au plafond bloque l'appel, sans repli), sinon
`base_url` / `api_key` du modèle en base, sinon variable d'environnement. Sur Nubo,
tous ces appels passent par le proxy de sortie et l'allowlist egress ; c'est la question à
poser en premier (ADR-088 §2.4).
