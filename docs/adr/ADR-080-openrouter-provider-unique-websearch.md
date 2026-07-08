# ADR-080 — OpenRouter comme provider plateforme unique + web search paramétrable par modèle

- **Statut** : **Accepté** (arbitrage Bertrand du 2026-07-08, post-spike
  [SPIKE-001](../spikes/SPIKE-001-openrouter-phase0.md) — voir §6 Amendements)
- **Date** : 2026-07-08
- **Branche** : `claude/openrouter-multi-tenant-4802kt` (greffée sur `multi-tenants`)
- **Épopée** : `docs/epics/EPIC-001-openrouter-multitenant.md`
- **ADR liés** : ADR-076 (historique inviolable), ADR-077 (tenancy/RBAC), ADR-078 (BYOK / pricing / usage / budget), ADR-079 (juges souverains)
- **Note** : à re-synchroniser dans le vault Obsidian `30-Knowledge/ADR/` (non accessible depuis le conteneur d'exécution).

---

## 1. Contexte

Aujourd'hui chaque fournisseur LLM est câblé **individuellement** :

- `llm_clients.client_for_model()` dispatche par « famille » (`openai`, `mistral`, `gemini`,
  `albert`, `generic`) avec un SDK et une clé par provider.
- `run.py::call_tested_llm()` contient **trois branches** provider-spécifiques pour le web search
  **natif** : OpenAI (`responses.create` + tool `web_search` + `user_location` Paris/FR),
  Mistral (API **Agents/Conversations** + tool `web_search`), Gemini (`GoogleSearch`).
- Les citations sont extraites par **regex** sur le texte (`run.py::extract_urls`), pas depuis
  les métadonnées de sources des API.
- La consommation (`webapp/usage.py`) est estimée par **heuristique** `tokens ≈ len/4` faute
  d'un coût réel homogène entre providers → le budget (`webapp/budget.py`) est donc approximatif.

Le socle multi-tenant (ADR-077/078/079) a déjà introduit ce qu'il faut pour un modèle « provider
unique » :

- résolution **en cascade** des credentials : `client_for_model(model, organization_id)` →
  `org_credentials` (BYOK chiffré Fernet) → `models.api_key` (plateforme) → variables d'env ;
- `usage` (row-par-appel, `billed_to = platform | byok`), `model_pricing` (versionné),
  `budgets` (plafond **mensuel** soft-stop), `estimate_scan_cost` (devis pré-scan) ;
- RBAC `org_admin | editor | viewer` + platform admin (`lab-team`).

**Vérification OpenRouter (2026-07)** — l'API expose un paramètre `engine` dans le plugin `web` :

- `engine: "native"` → **web search natif du provider** (OpenAI, Anthropic, Google/Gemini,
  Perplexity, xAI) ;
- `engine: "exa"` → recherche sémantique Exa (400+ modèles, dont ceux sans search natif) ;
- `engine: "firecrawl"` → moteur alternatif ;
- défaut/auto : `native` quand supporté, sinon `exa`.

Activation via `plugins:[{id:"web", engine, max_results, search_context_size, allowed_domains,
search_prompt}]`, ou `web_search_options:{search_context_size}`, ou suffixe `:online`. Les citations
reviennent **standardisées** en `annotations` de type `url_citation`
(`{url, title, content, start_index, end_index}`), identiques quel que soit le modèle. Prix Exa :
$4 / 1000 résultats (défaut 5 = $0.02 / requête) ; natif variable. OpenRouter renvoie le **coût réel**
dans `usage`.

Conséquence : le web search n'est plus un obstacle d'architecture, mais **un jeu de paramètres par
modèle** — rangeable dans la config existante de la table `models`.

## 2. Décision

Adopter **OpenRouter comme provider plateforme par défaut**, tout en conservant l'architecture
multi-tenant existante :

1. **Nouvelle famille `openrouter`** dans `llm_clients` : endpoint compatible OpenAI
   (`base_url = https://openrouter.ai/api/v1`), clé unique `OPENROUTER_API_KEY` en `.env`
   (chemin `platform` de la cascade). Le **BYOK par org reste l'override** naturel (cascade
   inchangée) ; on ne casse rien d'ADR-078.

2. **Web search = configuration par modèle.** Nouvelle colonne `models.search_config` (JSONB,
   idempotente via `migrations.sql`) :
   `{ "engine": "native|exa|firecrawl|off", "max_results": int, "search_context_size":
   "low|medium|high", "allowed_domains": [...] }`. Surfacée dans `model_form.html`.

3. **Collapse du dispatch web search — total** (amendé post-spike). Les trois branches
   provider-spécifiques de `run.py::call_tested_llm` convergent vers **un seul** appel
   `chat.completions.create(..., extra_body={"plugins":[{"id":"web", ...}]})` pour **tous les
   modèles testés** : OpenRouter est le **mode d'accès par défaut** (search natif pour OpenAI,
   **Exa assumé** pour Mistral/Gemini, cf. §6). Seuls les juges **souverains** (`is_sovereign`,
   Albert) gardent un chemin direct — ils ne font pas de web search.

4. **Citations structurées.** Les `url_citation` d'OpenRouter alimentent `raw_citations`
   (remplace/complète la regex `extract_urls`) — meilleure donnée pour les juges de citation.

5. **Coût réel → budget exact.** `usage.record` consomme le **coût réel** renvoyé par OpenRouter
   (`usage.cost` / tokens réels) au lieu de l'heuristique `len/4`. Le devis (`estimate_scan_cost`)
   peut s'appuyer sur le pricing OpenRouter.

6. **Budget journalier** ajouté **à côté** du mensuel (soft-stop) : `check_budget` gagne une
   fenêtre `date_trunc('day')` en plus de `date_trunc('month')`.

7. **Allowlist de modèles par organisation** (`org_models`) : l'admin pré-sélectionne les modèles
   proposés aux `editor`/`viewer` dans les formulaires lancer/planifier (`_run_selection.html`).

8. **Pré-remplissage catalogue + pricing** depuis `GET /api/v1/models` d'OpenRouter (écran admin) :
   alimente `models` et `model_pricing` sans saisie manuelle.

9. **Juges souverains (`is_sovereign`, Albert) conservés en direct** : OpenRouter est un
   intermédiaire hors souveraineté → ADR-079 §6 impose de ne pas les router via OpenRouter.

## 3. Points à valider AVANT bascule — MESURÉS (spike Phase 0, 2026-07-08)

Résultats complets : [SPIKE-001](../spikes/SPIKE-001-openrouter-phase0.md).

- **Geo-targeting FR : NON propagé.** `web_search_options.user_location` (Paris/FR) est ignoré
  par OpenRouter (sources 100 % US, y compris sur `gpt-4o-search-preview`). Atténuation forte
  mesurée : question **en français** (le cas GEOeval) → sources 100 % françaises. **Accepté**
  (arbitrage n° 2) avec traçage `run_meta.geo`.
- **Mistral : Exa** (mesuré : surcoût 0,005 $/req ; `engine:native` → 404). **Découverte** :
  `gemini-2.5-pro` est **aussi** refusé en natif et route sur Exa, malgré la doc OpenRouter.
  Seul OpenAI obtient le search natif. **Arbitrage n° 1 : Exa accepté** — tous les modèles
  testés passent par OpenRouter (cf. §6).
- **Continuité des séries.** Couture avant/après tracée dans `run_meta` :
  `{provider_route: "openrouter|direct", search_engine: "native|exa", geo: "none|user_location"}`
  — jamais de réécriture de l'historique (ADR-076).

## 4. Conséquences

**Positives**

- Une seule clé/config plateforme ; ajout d'un modèle = 1 ligne catalogue (pré-remplie).
- `run.py` simplifié (3 branches → 1) ; citations structurées ; **budget réellement exact**.
- Budget/jour + allowlist par org = les 2 demandes admin couvertes.

**Négatives / risques**

- Dépendance à un intermédiaire unique (OpenRouter) : point de défaillance et de facturation
  centralisé ; markup OpenRouter sur le web search Exa.
- Couture longitudinale majeure à la bascule (documentée dans `run_meta`) ; pour
  **Mistral et Gemini**, on mesure désormais la recherche Exa d'OpenRouter, pas celle du
  provider — limite sémantique **assumée** (arbitrage n° 1) et rendue visible dans les données
  (`run_meta.search_engine`).
- Les juges souverains restent hors OpenRouter → le dispatch garde 2 chemins (OpenRouter + direct
  souverain).
- Perte du geo-targeting `user_location` (non propagé) — compensée en pratique par les questions
  en français ; écart possible à la marge vs les runs OpenAI directs historiques.

## 5. Alternatives écartées

- **Statu quo (une clé par provider).** Rejeté : ne répond ni au budget/org ni à la pré-sélection
  centralisée ; friction d'ajout de modèles.
- **Garder Mistral/Gemini en direct, ne basculer qu'OpenAI** (reco initiale du spike). Écarté par
  arbitrage : maintenir 3 SDK/branches pour 2 modèles contredit l'objectif de simplification, et
  la valeur du benchmark reste la comparaison longitudinale **à conditions constantes et
  tracées** — Exa, une fois documenté dans `run_meta`, est une condition constante.
- **Tout router via OpenRouter, y compris Albert.** Rejeté : viole la souveraineté (ADR-079 §6).

## 6. Amendements post-spike (arbitrage Bertrand, 2026-07-08)

1. **OpenRouter = mode d'accès par défaut pour TOUS les modèles testés** (pas seulement OpenAI) :
   search natif quand disponible (`engine:"native"`, OpenAI), **Exa sinon** (Mistral, Gemini).
   Les branches directes OpenAI/Mistral/Gemini de `run.py` sont supprimées à terme (après une
   période de recouvrement) ; seul le chemin direct **souverain** (Albert, juges) est pérenne.
2. **Geo** : perte de `user_location` acceptée ; `run_meta.geo` trace la condition de chaque run.
3. **Devise : EUR.** Le coût réel OpenRouter (`usage.cost`, USD) est converti **à l'ingestion**
   via un taux de config `USD_EUR_RATE` (`.env`, défaut `0.88` — taux du 2026-07-08 : 0,875) ;
   le montant USD brut est conservé (`usage.cost_usd`) pour l'audit et un éventuel re-calcul.
   `model_pricing` (ADR-078) reste en EUR ; l'import catalogue OpenRouter (Phase 5) convertit au
   même taux et date chaque version de prix.
