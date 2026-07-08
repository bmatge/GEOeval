# SPIKE-001 — Phase 0 EPIC-001 : mesures OpenRouter (geo, Mistral, usage)

- **Date** : 2026-07-08
- **Épopée** : [EPIC-001](../epics/EPIC-001-openrouter-multitenant.md) — stories S0.1 / S0.2 / S0.3
- **ADR cadre** : [ADR-080](../adr/ADR-080-openrouter-provider-unique-websearch.md) (statut : Proposé)
- **Méthode** : ~12 appels réels `chat.completions` sur `https://openrouter.ai/api/v1`
  (clé de test), plugin `web`, `usage:{include:true}`. Script jetable + dumps JSON en annexe
  ([spike_openrouter.py](./spike-001/spike_openrouter.py)).

## TL;DR — les 3 verdicts

| Question (epic §Phase 0) | Verdict mesuré |
|---|---|
| S0.1 — geo-targeting FR propagé ? | **NON.** `web_search_options.user_location` (Paris/FR) est ignoré : sources 100 % US, y compris sur `gpt-4o-search-preview` qui supporte pourtant le paramètre chez OpenAI. **Atténuation forte** : une question posée en français produit des sources françaises (10/10 citations `.fr` sur un test actus). |
| S0.2 — Mistral natif ou Exa ? | **Exa** (confirmé par appel réel : surcoût de 0,005 $ pile = tarif Exa ; `engine:"native"` → erreur 404 explicite). |
| S0.3 — forme de `usage` ? | Complète et exploitable : `usage.cost` (USD), `cost_details.upstream_inference_cost` (+ détail prompt/completion), `is_byok`, `server_tool_use_details.web_search_requests` (natif). Coût Exa = `cost − upstream_inference_cost`. |

**Découverte hors périmètre prévu — la plus structurante** : `google/gemini-2.5-pro` est
**lui aussi refusé en search natif** (404 identique à Mistral) et route sur Exa, alors que la
doc OpenRouter annonce Google dans les providers natifs et que le catalogue affiche un pricing
`web_search` pour ce modèle. **Sur les 3 modèles testés du benchmark, seul OpenAI obtient
réellement le search natif via OpenRouter aujourd'hui.**

## Détail des mesures

### S0.1 — geo-targeting FR

| Test | Config | Résultat |
|---|---|---|
| `gpt-5.2` + `engine:native`, question actus EN, sans localisation | baseline | Sources US (apnews, wvnews) ; cadrage « (U.S. date) » |
| idem + `web_search_options.user_location` Paris/FR | propagation ? | **Toujours 100 % US** (apnews, click2houston) |
| `gpt-4o-search-preview` + `user_location` Paris/FR | modèle supportant officiellement le paramètre | **Toujours 100 % US** (foxnews, washingtonpost, CNN) |
| `gpt-5.2` + `engine:native`, question actus **en français** | atténuation par la langue | **10/10 citations françaises** (leparisien, tf1info, info.gouv.fr, assemblee-nationale.fr…) |

Conclusion S0.1 : la localisation n'est **pas propagée** par OpenRouter. En pratique, les
questions GEOeval étant des questions factuelles **en français**, le search natif suit la langue
de la requête et produit des sources françaises. C'est une **couture longitudinale** vis-à-vis
des runs actuels (appel OpenAI direct avec `user_location` Paris forcé) : à la marge, les
sources peuvent différer → traçage obligatoire dans `run_meta`
(`{provider_route, search_engine, geo: "none"}`), cf. S2.3 de l'epic et ADR-076.

Le contournement `search_prompt` (« user is located in Paris ») n'a pas montré d'effet
mesurable ; `allowed_domains: [".fr", ".gouv.fr"]` reste un levier disponible mais non testé
(attention : avec l'engine par défaut, poser des filtres de domaine fait retomber Google sur Exa
d'après la doc).

### S0.2 — Mistral (et Gemini !) : natif vs Exa

- `mistralai/mistral-large-2512` + plugin `web` (engine auto) : recherche OK, 5 citations
  françaises de qualité (service-public.gouv.fr, travail-emploi.gouv.fr…). Coût :
  `cost 0,0072395 $` − `upstream_inference_cost 0,0022395 $` = **0,005 $ = tarif Exa**.
- `engine:"native"` forcé → **404** : « The requested model does not support native web search ».
- **`google/gemini-2.5-pro` : exactement le même comportement** (404 en natif, Exa en auto,
  surcoût 0,005 $), malgré la doc et le pricing catalogue.

Conséquence : le choix binaire « Mistral via Exa OU Mistral en direct » prévu par l'ADR-080 §3
devient un **arbitrage à 2 modèles** (Mistral **et** Gemini). Rappel de l'enjeu (ADR-080 §5) :
avec Exa, on mesure la recherche d'OpenRouter, pas celle du modèle — ce qui casse la sémantique
GEO du benchmark pour ces modèles.

### S0.3 — structure `usage` (mapping `UsageRecord`)

```jsonc
{
  "prompt_tokens": 1908, "completion_tokens": 857, "total_tokens": 2765,
  "prompt_tokens_details": {"cached_tokens": 0},
  "cost": 0.0072395,                       // USD, TOTAL facturé (search Exa inclus)
  "is_byok": false,
  "cost_details": {
    "upstream_inference_cost": 0.0022395,  // inférence seule
    "upstream_inference_prompt_cost": 0.000954,
    "upstream_inference_completions_cost": 0.0012855
  },
  "server_tool_use_details": {"web_search_requests": 3}  // présent si search NATIF exécuté
}
```

- Mapping direct vers `UsageRecord` : `prompt_tokens` / `completion_tokens` réels + coût réel.
- **Devise : USD** — le socle ADR-078 stocke `cost_eur` → décider : conversion à l'ingestion
  (taux fixé en config) ou colonne `cost_usd` dédiée.
- Le format citations est homogène (`annotations[].url_citation{url, title, start_index,
  end_index, content?}`) — `content` est **absent en natif OpenAI**, fourni par Exa → optionnel.
- ⚠️ Coût du natif OpenAI : les résultats de recherche gonflent les `prompt_tokens`
  (jusqu'à 51 k tokens et 0,24 $ observés sur une question d'actualités, 10 recherches).
  Sur une question factuelle simple : 0,01 – 0,08 $. Le devis (`estimate_scan_cost`) devra
  s'appuyer sur des coûts réels observés, pas seulement le pricing par token.

## Décisions à arbitrer (Bertrand)

1. **Mistral & Gemini : direct ou Exa ?** Reco : **garder les deux en direct** (chemins
   existants conservés, prévu par ADR-080 §2.3) tant que leur search natif n'est pas exposé par
   OpenRouter ; OpenAI bascule seul sur OpenRouter en Phase 2. Le catalogue OpenRouter reste
   utilisable pour ajouter d'autres modèles à search natif (Anthropic, xAI, Perplexity).
2. **Geo : accepter la perte de `user_location`** pour les modèles routés OpenRouter (questions
   FR = sources FR en pratique), avec traçage `run_meta.geo` — ou maintenir OpenAI en direct
   aussi, ce qui viderait la Phase 2 de sa substance.
3. **Devise** : conversion USD→EUR à l'ingestion (taux en config) ou stockage `cost_usd`.

Si l'arbitrage n° 1 est retenu, **amender l'ADR-080** (§2.3 et §3) avant de la passer
« Acceptée » : la promesse « collapse des 3 branches en 1 » devient « OpenAI via OpenRouter +
Mistral/Gemini/Albert en direct », soit 2 chemins durables et non transitoires.

## Impact sur le séquencement de l'epic

- **Phase 1 (fondations `openrouter`, juges, coût réel) : inchangée** — rien dans le spike ne
  la remet en cause ; les juges n'ont pas besoin de web search.
- **Phase 2 : re-cadrée** par l'arbitrage n° 1 (S2.2 ne collapse que la branche OpenAI).
- Phases 3/4/5 : inchangées.
