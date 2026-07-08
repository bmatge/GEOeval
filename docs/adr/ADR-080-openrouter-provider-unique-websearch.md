# ADR-080 — OpenRouter comme provider plateforme unique + web search paramétrable par modèle

- **Statut** : Proposé (décision finale : Bertrand, via PR)
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

3. **Collapse du dispatch web search.** Les trois branches provider-spécifiques de
   `run.py::call_tested_llm` convergent vers **un seul** appel `chat.completions.create(...,
   extra_body={"plugins":[{"id":"web", ...}]})`. Les modèles non-OpenRouter (souverains, legacy)
   gardent leur chemin direct tant qu'ils existent au catalogue.

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

## 3. Points à valider AVANT bascule (spike, cf. epic Phase 0)

Ces points touchent l'intégrité **longitudinale** (ADR-076) ; ils se **mesurent**, ne se supposent pas :

- **Geo-targeting FR.** L'appel OpenAI actuel force `user_location = {country:FR, city:Paris,
  timezone:Europe/Paris}` — déterminant pour un benchmark français. Vérifier qu'OpenRouter propage
  bien la localisation (`web_search_options` / `plugins`) ; sinon, écart de résultats.
- **Mistral.** Son search natif passe par l'API **Agents** et Mistral **n'est pas** dans la liste
  « native » d'OpenRouter → via OpenRouter, un modèle Mistral risque de basculer sur **Exa**.
  Arbitrer : accepter Exa pour Mistral, ou garder Mistral en direct.
- **Continuité des séries.** Tout changement de couche de recherche crée une couture avant/après.
  Tracer dans `run_meta` : `{provider_route: "openrouter|native", search_engine, geo}` pour garder
  les runs interprétables (jamais de réécriture de l'historique — ADR-076).

## 4. Conséquences

**Positives**

- Une seule clé/config plateforme ; ajout d'un modèle = 1 ligne catalogue (pré-remplie).
- `run.py` simplifié (3 branches → 1) ; citations structurées ; **budget réellement exact**.
- Budget/jour + allowlist par org = les 2 demandes admin couvertes.

**Négatives / risques**

- Dépendance à un intermédiaire unique (OpenRouter) : point de défaillance et de facturation
  centralisé ; markup OpenRouter sur le web search Exa.
- Couture longitudinale à documenter ; cas Mistral potentiellement dégradé (Exa vs Agents).
- Les juges souverains restent hors OpenRouter → le dispatch garde 2 chemins (OpenRouter + direct).

## 5. Alternatives écartées

- **Statu quo (une clé par provider).** Rejeté : ne répond ni au budget/org ni à la pré-sélection
  centralisée ; friction d'ajout de modèles.
- **OpenRouter + moteur Exa uniforme pour tous.** Rejeté : casse la sémantique GEO (on mesurerait la
  recherche d'OpenRouter, pas celle du modèle) et la continuité longitudinale. `engine:"native"`
  évite précisément ce piège.
- **Tout router via OpenRouter, y compris Albert.** Rejeté : viole la souveraineté (ADR-079 §6).
