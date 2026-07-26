# Backend d'inférence

Contenu déplacé tel quel depuis README.md (chantier restructuration, voir docs/briefs/restructuration-et-anglais.md, phase 3) — pas de réécriture à ce stade.

Le backend par défaut est **TabbyAPI** (image officielle
[`ghcr.io/theroyallab/tabbyapi`](https://github.com/theroyallab/tabbyAPI),
backend ExLlamaV3), servant **Qwen3.6-27B en quantisation EXL3** (variante
VL, vision préservée pour GhostDesk/OCR — voir Images et thinking adaptatif
et OCR d'appoint plus bas), avec **MTP natif** (`draft_mode: mtp` dans
`services/tabbyapi/config.yml`, tête de prédiction multi-token du modèle
lui-même, pas de modèle de draft séparé à charger).

Config `services/tabbyapi/config.yml` (montée en lecture seule) : champs clés
`model_dir`/`model_name` (répertoire HuggingFace-style du quant EXL3 sous
`./models`, **pas** un `.gguf` — voir plus bas), `backend: exllamav3`,
`cache_mode`/`cache_size`/`max_seq_len` (à affiner selon la VRAM disponible
cumulée sur les deux GPU), `draft_model.draft_mode: mtp`, `tool_format`, et
trois déviations volontaires par rapport aux défauts TabbyAPI :
`disable_auth: true` (réseau interne `agent-net` uniquement, même modèle de
confiance que `llama-server`/Ollama), `vision: true` (désactivé par défaut
même si le modèle a des capacités vision) et
`reasoning: true` (désactivé par défaut chez TabbyAPI, requis pour parser
les blocs `<think>` de Qwen).

Modèle cible : fichiers HuggingFace-style (safetensors + `config.json` +
tokenizer) attendus sous `./models/agent-llm/` (ou `MODELS_HOST_PATH`) —
**jamais téléchargés automatiquement**, comme pour `llama-server`. Le nom
`agent-llm` (plutôt que le nom réel du dépôt HuggingFace téléchargé) est
requis pour matcher le `model="agent-llm"` en dur dans `ChatOpenAI`
(`services/langgraph-agent/app/graph.py`) sans toucher au code — même
convention que l'aliasing Ollama plus bas (`scripts/rebuild-agent-llm.sh`).


## Images et thinking adaptatif (`services/langgraph-agent/app/graph.py`)

**Conversion d'images** (`IMAGE_FORMAT_PASSTHROUGH`, variable d'env, défaut
absent = conversion PNG) : `_to_png_data_uri` reste le chemin par défaut —
chaque résultat image d'outil (`screen_shot` GhostDesk, WebP natif) est
systématiquement reconverti en PNG avant transmission au LLM. C'est le
défaut pour le backend TabbyAPI (ExLlamaV3 n'est pas connu pour décoder le
WebP nativement — à vérifier empiriquement, voir Backend d'inférence
plus haut) comme pour Ollama (décodeur mtmd, échec explicite sur le WebP).

**Rétention d'images** (`MAX_IMAGES_IN_CONTEXT`, variable d'env, défaut `1`) :
seules les `MAX_IMAGES_IN_CONTEXT` dernières captures d'écran restent en
blocs `image_url` multimodaux dans l'historique soumis au LLM à chaque
appel ; les précédentes sont remplacées par le texte indicatif
`[screenshot antérieure supprimée]` (`_apply_image_retention`). **Ne touche
jamais au checkpointer** : ce filtrage ne s'applique qu'à la liste de
messages construite juste avant `bound_llm.astream()`, jamais à
`state["messages"]` lui-même — l'historique complet, avec toutes les images
d'origine, reste intact et rejouable (ex. si `MAX_IMAGES_IN_CONTEXT` change
d'une conversation à l'autre). Motivation : une boucle capture/clic
GhostDesk répétée peut accumuler de nombreuses captures dans l'historique,
chacune coûteuse en tokens visuels, pour un intérêt quasi nul au-delà de la
plus récente (seule reflète l'état actuel de l'écran).

**Thinking adaptatif** (`ADAPTIVE_THINKING`, variable d'env, défaut `false`) :
Qwen3.6 raisonne par défaut sur chaque tour (balises de pensée étendue),
coûteux en latence pour une boucle perception-action rapide où chaque tour
n'a qu'à décider "où cliquer ensuite". Si activé, `_apply_adaptive_thinking`
ajoute un system prompt transitoire `/no_think` (lui aussi jamais persisté
dans l'état du graphe, même principe que la rétention d'images ci-dessus)
quand **tous** les tool_calls du tour précédent étaient auto-approuvés
(même politique par tiers que `has_tool_calls`, grants de session inclus —
voir `approval_policy.py`). Pas d'injection sur le tout premier tour d'une
tâche (aucun tool_calls précédent à évaluer) ni dès qu'un outil sensible
était en jeu dans ce tour précédent : le raisonnement complet y garde toute
sa valeur.

