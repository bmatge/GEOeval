# EPIC-001 — Bascule OpenRouter (provider plateforme unique) sur socle multi-tenant

- **Statut** : Proposé
- **Décision cadre** : [ADR-080](../adr/ADR-080-openrouter-provider-unique-websearch.md)
- **Branche de travail** : `claude/openrouter-multi-tenant-4802kt` (greffée sur `multi-tenants`)
- **Cible de merge** : `multi-tenants`
- **Suivi** : issues GitHub désactivées sur ce repo → cet epic + `todo.md` font foi (PR par story).

## Objectif

Passer d'un modèle « chaque fournisseur paramétré individuellement » à **OpenRouter comme provider
plateforme unique** (clé en `.env`, dispatch transparent), sans casser le socle multi-tenant
existant (BYOK, usage, pricing, budget, RBAC) ni l'intégrité longitudinale (ADR-076). Livrer en
prime les deux demandes admin : **budget max / org / jour** et **pré-sélection des modèles** proposés
aux éditeurs/viewers (avec pré-remplissage depuis le catalogue OpenRouter).

## Résultat attendu (Definition of Done de l'epic)

- [ ] Un modèle OpenRouter fonctionne de bout en bout (run + juge) avec clé unique `.env`.
- [ ] Le web search est configurable **par modèle** (native/exa/firecrawl/off) via l'UI Modèles.
- [ ] Le coût réel OpenRouter alimente `usage` → budget **mensuel ET journalier** exacts.
- [ ] Un `org_admin` pré-sélectionne les modèles visibles par ses `editor`/`viewer`.
- [ ] Un admin plateforme pré-remplit catalogue + pricing depuis OpenRouter.
- [ ] Les juges souverains (Albert) restent en direct ; historique jamais réécrit.
- [ ] Testé sur le stack Docker local (build + up + curl des pages touchées) avant chaque PR.

## Périmètre / hors périmètre

**Dans** : famille `openrouter`, config web search par modèle, coût réel, budget/jour, allowlist
org↔modèles, pré-remplissage catalogue/pricing. **Hors** : refonte des juges souverains, migration
des runs historiques (préservés tels quels), suppression des familles legacy (désactivation seulement).

---

## Phases & stories

Chaque story = une PR indépendante vers `multi-tenants`, avec ses critères d'acceptation (CA).

### Phase 0 — Spike de validation (bloquant, aucune bascule avant)

> But : **mesurer** les 2 inconnues qui conditionnent tout (cf. ADR-080 §3). Livrable = note de spike
> + décision Mistral, pas de code de prod.

- **S0.1 — Geo-targeting FR via OpenRouter.**
  - CA : script jetable appelant OpenRouter `chat.completions` + `plugins:[{id:web,engine:native}]`
    sur une question dont la réponse dépend de la localisation FR ; comparer les sources obtenues
    à l'appel OpenAI direct actuel (`user_location` Paris). Conclusion tranchée : localisation
    propagée OUI/NON, et si NON, contournement (search_prompt, allowed_domains `.fr`, …).
- **S0.2 — Mistral : natif vs Exa.**
  - CA : vérifier sur la page modèle OpenRouter + un appel réel si un modèle Mistral route en
    `native` ou retombe en `exa`. Décision documentée : Mistral via OpenRouter (Exa accepté) OU
    Mistral gardé en direct (Agents).
- **S0.3 — Forme de `usage`/coût réel.**
  - CA : capturer la structure exacte du champ coût/tokens renvoyé par OpenRouter (`usage`),
    valider le mapping vers `UsageRecord` (input/output tokens + `cost_eur`).

### Phase 1 — Fondations OpenRouter (provider + coût réel + citations)

- **S1.1 — Famille `openrouter` dans `llm_clients`.**
  - Ajouter `openrouter` à `_FAMILY_BY_NAME` + branche client (OpenAI-compatible,
    `base_url=https://openrouter.ai/api/v1`, `api_key or os.environ["OPENROUTER_API_KEY"]`,
    en-têtes `HTTP-Referer`/`X-Title` recommandés).
  - `.env.example` : ajouter `OPENROUTER_API_KEY`.
  - CA : `client_for_model()` renvoie un client OpenRouter ; cascade BYOK→plateforme→env inchangée.
- **S1.2 — Juges via OpenRouter.**
  - `evaluate.py::call_judge_llm` : router les modèles `model_name="openrouter"` par le chemin
    `chat.completions` existant (déjà utilisé par Albert/`openai-compatible`).
  - CA : un juge OpenRouter note un run et écrit `run_evaluations` (JSON strict respecté).
- **S1.3 — Coût réel dans `usage`.**
  - `webapp/usage.py::record` : accepter un coût/tokens **réels** (issus de la réponse OpenRouter)
    ; fallback heuristique `len/4` conservé pour les providers directs.
  - CA : une ligne `usage` OpenRouter porte le coût réel (≠ heuristique) ; `billed_to` correct.

### Phase 2 — Web search paramétrable + simplification de `run.py`

- **S2.1 — Colonne `models.search_config` (JSONB).**
  - `migrations.sql` : `ADD COLUMN IF NOT EXISTS search_config JSONB`. ORM `Model` + `model_form.html`
    (engine, max_results, search_context_size, allowed_domains).
  - CA : édition/lecture du search_config depuis l'UI Modèles ; valeurs par défaut sûres.
- **S2.2 — Appel unifié web search.**
  - `run.py::call_tested_llm` : pour `model_name="openrouter"`, un seul
    `chat.completions.create(..., extra_body={"plugins":[{"id":"web", **search_config}]})`.
    Branches OpenAI/Mistral/Gemini directes **conservées** pour les modèles non-OpenRouter.
  - CA : un run OpenRouter en `engine:native` produit réponse + citations.
- **S2.3 — Citations structurées + traçage longitudinal.**
  - Alimenter `raw_citations` depuis les `annotations` `url_citation` ; enrichir `run_meta`
    (`provider_route`, `search_engine`, `geo`).
  - CA : `raw_citations` peuplé sans regex pour un run OpenRouter ; `run_meta` documente le moteur.

### Phase 3 — Budget journalier

- **S3.1 — Fenêtre journalière dans le budget.**
  - `models.Budget` : ajouter `daily_cap_eur` (nullable) ; `migrations.sql` idempotent.
  - `webapp/budget.py` : `current_period_spent(org, period)` (`day` | `month`) ;
    `check_budget` refuse si `spent_day+estimate > daily_cap` **ou** `spent_month+estimate > monthly_cap`
    (soft-stop, ADR-078 : un scan en cours va au bout).
  - CA : un cap/jour dépassé bloque un **nouveau** scan ; le mensuel reste opérationnel.
- **S3.2 — UI admin budget/jour.**
  - `org_budget.html` : champ cap journalier + affichage `dépensé aujourd'hui / plafond`.
  - CA : `org_admin` pose un cap/jour ; le devis affiche l'impact jour + mois.

### Phase 4 — Allowlist de modèles par organisation

- **S4.1 — Table `org_models`.**
  - ORM + `migrations.sql` : `(organization_id, model_id, is_active)`,
    UNIQUE `(organization_id, model_id)`.
  - CA : DAO CRUD ; org sans allowlist ⇒ comportement rétro-compatible (à trancher : tout visible
    OU rien — défaut proposé : héritage catalogue global filtré `is_active`).
- **S4.2 — Filtrage dans les formulaires.**
  - `_run_selection.html` (lancer/planifier) : ne proposer aux `editor`/`viewer` que les modèles de
    l'allowlist ; `org_admin` gère l'allowlist.
  - CA : un `editor` ne voit que les modèles autorisés ; un `org_admin` édite la liste.

### Phase 5 — Pré-remplissage catalogue + pricing depuis OpenRouter

- **S5.1 — Import catalogue.**
  - Écran admin : fetch `GET /api/v1/models` → propose la création/màj de lignes `models`
    (`model_name="openrouter"`, `model_version=<id OpenRouter>`).
  - CA : depuis la liste OpenRouter, un admin crée un modèle en 1 clic (sans saisie manuelle d'URL).
- **S5.2 — Import pricing.**
  - Alimenter `model_pricing` (versionné, ADR-078) depuis le pricing OpenRouter (clôt l'ancienne row,
    en crée une neuve).
  - CA : le pricing d'un modèle importé est renseigné et daté ; le devis l'utilise.

---

## Risques & garde-fous

- **Continuité longitudinale (ADR-076)** : jamais de réécriture des runs ; couture avant/après tracée
  dans `run_meta`. Garde-fou = S2.3.
- **Geo-targeting FR** : bloquant si non propagé → décision en S0.1 avant Phase 2.
- **Souveraineté (ADR-079)** : Albert jamais routé via OpenRouter (S1.2 exclut `is_sovereign`).
- **Point de défaillance unique** : OpenRouter down ⇒ prévoir repli sur chemins directs conservés.
- **Secrets** : `OPENROUTER_API_KEY` et `GEOEVAL_KEY_SECRET` uniquement en `.env` /
  `/opt/apps/geoeval/.env` ; jamais en clair dans le repo ou les logs.

## Séquencement recommandé

`Phase 0` (spike) → `Phase 1` (fondations) → `Phase 2` (web search) → `Phase 3` (budget/jour) →
`Phase 4` (allowlist) → `Phase 5` (catalogue). Phases 3/4/5 sont parallélisables une fois la Phase 1
mergée.
