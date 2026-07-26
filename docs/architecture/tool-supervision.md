# Supervision humaine des appels d'outils

Contenu déplacé tel quel depuis README.md (chantier restructuration, voir docs/briefs/restructuration-et-anglais.md, phase 3) — pas de réécriture à ce stade.

Tout appel d'outil demandé par le LLM (`terminal`, `filesystem`, `git`,
`browser`, `desktop`/GhostDesk) suspend le graphe LangGraph au lieu de
s'exécuter automatiquement (nœud `require_approval`,
`services/langgraph-agent/app/graph.py`). L'agent répond alors dans la
conversation avec un message `⚠️ Approbation requise pour : ...` proposant
trois réponses : "approuver" (une fois), "approuver pour la session" (voir
Grants de session plus bas) ou "refuser" (un `ToolMessage` d'erreur "Rejeté
par l'utilisateur" est renvoyé au LLM, qui peut réagir normalement).

**Politique par tiers de réversibilité** (`services/langgraph-agent/app/
approval_policy.py`), qui remplace l'ancienne whitelist binaire :

| Tier | Comportement | Exemples par défaut |
|---|---|---|
| `TIER_READ` (lecture) | auto, silencieux | `screen_shot`, `mouse_move`, `app_list`, `app_running`, `app_status`, lecture filesystem/git (`read_file`, `git_status`, `git_log`...), `run_command` (mcp-terminal, déjà une liste blanche stricte en lecture seule) |
| `TIER_REVERSIBLE` (réversible) | auto + journalisation (voir Phase 2, journal d'audit) | `mouse_click`, `mouse_double_click`, `mouse_drag`, `mouse_scroll`, `key_press`, `app_launch`, `clipboard_set`, écritures filesystem/git confinées (`write_file`, `git_commit`...) |
| `TIER_SENSITIVE` (sensible) | approbation humaine requise | `key_type` (saisie de texte libre), tout le reste, **et tout outil inconnu** |

**`NEVER_GRANTABLE_TOOLS`** (Phase 1d-révisée, voir docs/history.md, T5) :
`browser_run_code_unsafe` et `browser_evaluate` restent `TIER_SENSITIVE`
même accordés "pour la session" — un grant assouplit normalement un outil
sensible en réversible pour le reste du thread, mais l'exécution de code
arbitraire dans la page est une élévation, pas une primitive de lecture ;
chaque appel de ces deux outils requiert une approbation individuelle,
sans exception.

**`browser_extract`** (Phase 1d-révisée, voir docs/history.md "correctif
extraction") : constaté en conditions réelles que rendre `browser_evaluate`
non-accordable a fait disparaître son usage (T1/T10) sans remplacement —
remplacé par une exploration manuelle nettement moins fiable (ctrl+f,
parcours page par page). `browser_extract(query)` (outil synthétique,
`services/mcp-client/app/main.py`) donne la capacité manquante — chercher
un texte dans la page et obtenir son contexte — via un template JS FIXE
(la requête est interpolée par `json.dumps`, jamais concaténée en code
exécutable), tier `TIER_READ` : le modèle ne fournit jamais de code, donc
aucune élévation, contrairement à `browser_evaluate` qui reste lui
`NEVER_GRANTABLE`.

**Règles sur arguments** (Phase 4, `RULES`/`_load_rules` dans
`approval_policy.py`, format `outil(pattern)` à la Claude Code) : affinent
le tier d'un outil selon SES ARGUMENTS plutôt que son seul nom. Implémentées
comme des matchers nommés en Python (pas de DSL de pattern générique), pas
comme une simple ANDition avec le tier statique — une règle qui matche
l'emporte entièrement sur `tool_tier()`. Règle par défaut :
`key_type(len<50,no_newline)` → `TIER_REVERSIBLE` (saisie courte et
mono-ligne, assez anodine pour ne pas justifier une approbation à chaque
frappe), alors que `key_type` reste `TIER_SENSITIVE` par défaut pour tout le
reste (texte long ou multi-lignes — script collé, code...). Un matcher
`command_prefix` est aussi fourni (préfixes de commande, ex. pour
`run_command` côté mcp-terminal) mais sans règle par défaut, ce serveur
n'exposant déjà qu'une liste blanche en lecture seule. En cas d'ambiguïté
(plusieurs règles nommées pour le même outil matchent à la fois), le tier
le plus restrictif gagne. `APPROVAL_RULES_PATH` (variable d'env, optionnel)
pointe vers un fichier YAML qui complète ces règles par défaut (jamais ne
les remplace) — voir `_load_rules_from_yaml` pour le format exact
(`tool`/`matcher`/`tier`, `command_prefix` prenant en plus `prefixes`).

Le défaut est toujours le tier le plus restrictif, jamais l'inverse : un
outil qui n'apparaît dans aucune des listes `TIER_READ_TOOLS`/
`TIER_REVERSIBLE_TOOLS` (surchargeables via ces variables d'env,
CSV) est automatiquement `TIER_SENSITIVE`. Routage dans `has_tool_calls` :
un tour dont **tous** les tool_calls sont en tier lecture ou réversible
saute `require_approval` ; un tour mixte (même un seul outil sensible)
reste entièrement soumis à approbation, par sécurité — pas d'approbation
partielle par outil.

`AUTO_APPROVED_TOOLS` (ancienne variable d'env) reste utilisable comme
override rétrocompatible : tout outil qui y figure est traité comme
`TIER_REVERSIBLE` même s'il n'est dans aucune des deux listes ci-dessus.
Vide par défaut désormais — les anciens défauts historiques (`app_list,
app_running,screen_shot,mouse_move,mouse_click,mouse_double_click,
mouse_drag,mouse_scroll`) sont déjà couverts par les tiers par défaut
ci-dessus, donc ce nouveau défaut vide reproduit le même comportement pour
un déploiement qui ne fixe pas cette variable.

Une exclusion volontaire malgré son nom trompeur : `clipboard_get` reste
`TIER_SENSITIVE` malgré son nom de "lecture" — il peut exfiltrer des
données sensibles copiées par l'utilisateur (mot de passe, jeton...), pas
moins sensible que `clipboard_set`.

`key_type`/`key_press` restent hors `TIER_READ`, mais une **suite** de
`mouse_click` auto-approuvés peut en théorie composer n'importe quelle
saisie via un clavier virtuel à l'écran, contournant de fait cette
exclusion — voir `AUTO_APPROVAL_STREAK_LIMIT` juste en dessous, qui
s'applique à tout outil auto-approuvé (tier lecture ou réversible), pas
seulement à l'ancienne liste `AUTO_APPROVED_TOOLS`.

**Garde-fou contre le clavier virtuel** (`AUTO_APPROVAL_STREAK_LIMIT`,
variable d'env, défaut `6`) : au-delà de ce nombre de tours auto-approuvés
consécutifs *sans passage par un humain*, `has_tool_calls` force le tour
suivant à repasser par `require_approval` — même s'il ne contient que des
outils normalement auto-approuvés. Compteur `auto_approval_streak` dans
`AgentState`, incrémenté à chaque tour exécuté (`call_tools`) et remis à 0
dès qu'un humain valide réellement une approbation (`require_approval`,
uniquement lors de la reprise, pas pendant la pause). Distinct de
`tool_iterations`/`MAX_TOOL_ITERATIONS`, qui mesure un budget total pour
toute la tâche et non un nombre de tours *consécutifs sans supervision*.

**Grants de session** (Phase 3, `AgentState.session_grants` dans
`app/graph.py`) : répondre "approuver pour la session" plutôt que
"approuver" ajoute le(s) outil(s) du tour en attente à une liste
`session_grants` propre à ce thread. Un outil qui y figure est ensuite
plafonné à `TIER_REVERSIBLE` (auto + audit, voir Phase 2 ci-dessous) pour le
reste de la conversation — `approval_policy.effective_tier()` en tient
compte en plus du tier statique de l'outil. Un grant ne s'applique jamais
rétroactivement : le tour qui le demande reste soumis à CETTE approbation,
seuls les appels *suivants* du même outil en profitent. Portée strictement
par outil : accorder `key_type` ne dispense pas `browser_navigate`.

Ces grants vivent dans l'état du graphe, donc dans le même checkpointer
`MemorySaver` (en mémoire uniquement, voir section Persistance des données)
que le reste du thread — **ils meurent avec lui** : un redémarrage du
service les perd exactement comme il perd une approbation en attente,
puisqu'il n'existe aucune distinction entre "perdre l'état du thread" et
"perdre les grants qu'il contenait". Comportement voulu pour un usage
local : pas de persistance de grants inter-redémarrage, chaque nouvelle
conversation (ou reprise après redémarrage) repart sans historique
d'approbation.

**Journal d'audit** (Phase 2, `services/langgraph-agent/app/audit_log.py`,
angle mort corrigé — voir docs/history.md, investigation T9) : chaque tool_call
effectivement exécuté dont le tier n'est pas `TIER_READ` (silencieux par
design, rien de nouveau à auditer) est loggé en JSONL sous `AUDIT_LOG_DIR`
(défaut `/workspace/.audit`, même bind mount que les serveurs MCP
filesystem/git/terminal — voir `docker-compose.yml`), un fichier par jour
(`YYYY-MM-DD.jsonl`). Chaque ligne : `timestamp`, `thread_id`, `tool`,
`arguments`, `tier`, `result` (le résultat de l'outil TEL QUE VU PAR LE
MODÈLE — déjà tronqué/hiérarchisé si `browser_*`, jamais la version brute ;
ajouté en Phase 1d-révisée, voir docs/history.md, pour reconstruire non
seulement la séquence d'appels mais aussi ce que l'agent a réellement perçu
à chaque étape). Rotation par volume en plus du fichier quotidien :
au-delà de `AUDIT_LOG_MAX_BYTES` (défaut 20 Mio), le fichier du jour est
compressé (`.N.jsonl.gz`) avant la prochaine écriture — `read_entries`/
`GET /audit` relisent les archives compressées de façon transparente.

**Avant ce correctif**, seul un tool_call arrivé directement depuis
`has_tool_calls` (sans passer par `require_approval` ce tour-ci) était
audité — l'hypothèse étant qu'un tour passé par `require_approval` a déjà
un humain dans la boucle, déjà tracé dans l'historique de conversation
("⚠️ Approbation requise" + la réponse), donc inutile à dupliquer.
Hypothèse fausse en pratique : en campagne automatisée,
`_approve(..., grant_session=True)` (le harnais de tests) joue ce rôle sans
qu'aucun humain ne regarde jamais, et l'historique de conversation lui-même
ne survit pas à un redémarrage du service (checkpointer `MemorySaver`, en
mémoire uniquement — voir Persistance des données plus bas) : le journal
d'audit reste alors la SEULE trace persistante. Le tout premier appel de
chaque outil par thread — le plus utile à l'investigation — restait donc
invisible, même en campagne. Désormais, tout tool_call passé par
`require_approval` est audité lui aussi, avec son tier réel (y compris
`TIER_SENSITIVE`) — `GET /audit?thread_id=...` (optionnel, sans lui renvoie
tout le journal disponible) permet la consultation ; une ligne corrompue
individuelle est ignorée à la lecture plutôt que de faire échouer toute la
requête.

**Messages assistant** (Phase 1d-révisée, voir docs/history.md "OBSERVABILITÉ") :
`call_llm` journalise aussi CHAQUE tour du modèle (`audit_log.log_message`,
`kind: "message"`, `role: "assistant"`, `content: {content, tool_calls}`) —
raisonnement `<think>` et texte inclus, tool_calls éventuels — sans
filtrage par tier, contrairement aux tool_calls ci-dessus : c'est le
raisonnement de l'agent, pas un effet de bord à sélectionner. Comble une
limite qui a concrètement bloqué un diagnostic d'archive (T1/T7/T10, voir
docs/history.md) : avant cet ajout, l'archive ne permettait de reconstruire QUE
la séquence d'appels et leurs résultats, jamais ce que le modèle avait
lui-même raisonné ou répondu à chaque étape.

**Isolation entre tâches** (Phase 1d-révisée, voir docs/history.md "isolation
entre tâches") : `playwright-mcp` est une session MCP PERSISTANTE et
PARTAGÉE par tout mcp-client (pas scopée par thread ni par tâche) — un
onglet laissé ouvert par une tâche reste visible dans le snapshot d'une
tâche suivante totalement différente, potentiellement des heures plus
tard. `POST /reset-session/{server_name}` (mcp-client) jette la session en
cache (le prochain appel en rouvre une neuve) ; le harnais de tâches web
l'appelle avant chaque répétition (voir `tests_integration/
test_web_tasks.py`, `_reset_browser_session`).

Même problème, canal différent (investigation T9, voir docs/history.md) :
GhostDesk pilote un vrai bureau à l'échelle de la MACHINE (`app_launch`),
sans aucun rapport avec la session Playwright ci-dessus ni avec le thread
en cours — une fenêtre laissée ouverte par une tâche reste lisible (via
`screen_shot`) par une tâche suivante des heures plus tard. `_reset_
ghostdesk_desktop()` (`pkill -f firefox` sur le conteneur `ghostdesk`)
appelé avant chaque répétition, même garantie que le reset Playwright.

**Approbation par bouton d'UI, sans passer par un message texte** : deux
endpoints complètent le flux texte "approuver"/"approuver pour la
session"/"refuser" —

- `POST /pending` (lecture seule, ne modifie aucun état) : indique si le
  thread dérivé de `messages` est en pause d'approbation, et renvoie le
  texte de la demande. Ne dépend que du premier message humain (dérivation
  du `thread_id`), jamais du contenu du dernier message assistant — celui-ci
  peut être vide ou tronqué côté client selon la façon dont Open WebUI
  interprète les balises `<think>`.
- `POST /approve` (`{"messages": [...], "approved": bool, "grant_session":
  bool}`) : reprend le thread en pause directement depuis une décision hors
  bande (Open WebUI Action function), en éditant en place le message "⚠️
  Approbation requise" existant plutôt qu'en ajoutant un nouveau message —
  d'où un bookkeeping de `owui_message_count` sans le `+1` appliqué au flux
  texte normal. `grant_session` (optionnel, défaut `false`, ignoré si
  `approved=false`) est le miroir de "approuver pour la session" pour ce
  flux hors bande. Renvoie 409 s'il n'y a aucune approbation en attente pour
  ce thread.

**Correctif streaming** : quand le modèle raisonne (balises `<think>`) avant
de décider d'un appel d'outil, le tour se termine avec un `content` réel
vide (le tool_call passe par un canal séparé), donc aucun chunk de contenu
ne referme jamais la balise côté client. Sans correctif, le texte
d'approbation qui suit se retrouvait concaténé à l'intérieur du `<think>`
resté ouvert — invisible en dehors de la bulle de pensée repliée d'Open
WebUI. `_stream_response` (`app/main.py`) referme désormais la balise avant
d'émettre ce texte, en se basant sur ce qui a réellement été streamé au
client (pas sur l'état déjà réparé en interne par `call_llm`).

Comme Open WebUI ne fournit pas d'identifiant de conversation stable à
`/v1/chat/completions` (il renvoie juste l'historique complet à chaque
appel), le thread LangGraph associé est retrouvé en dérivant un `thread_id`
déterministe à partir du hash du premier message de la conversation
(`_derive_thread_id`, `services/langgraph-agent/app/main.py`). **Limite
assumée** : deux conversations distinctes commençant par un message
strictement identique partageraient le même thread — acceptable pour un
usage local mono-utilisateur, pas au-delà. Un vrai correctif existerait côté
Open WebUI (écrire une "Pipe function" qui récupère son `chat_id` interne et
le transmet en amont) mais Open WebUI ne transmet actuellement pas cette
métadonnée à un backend OpenAI-compatible externe comme celui-ci (limitation
connue et documentée par le projet, non résolue à ce jour :
[discussion #6999](https://github.com/open-webui/open-webui/discussions/6999)).

Puisque ce thread persiste maintenant sur toute la durée d'une conversation
(pas seulement pendant une pause d'approbation), et qu'Open WebUI renvoie à
chaque tour l'historique complet en plus de ce qui est déjà persisté,
`owui_message_count` (champ de l'état du graphe) retient combien de messages
Open WebUI ont déjà été intégrés — seul le nouveau message est alors soumis
au tour suivant, ce qui évite de dupliquer l'historique (bug réellement
rencontré et corrigé pendant le développement, voir le tableau plus haut).

Aucune version de dépendance n'a été modifiée pour implémenter cette
fonctionnalité : `langgraph==0.2.34` (déjà pinné) fournissait déjà
`NodeInterrupt`, `MemorySaver` et les méthodes async `aget_state`/`aupdate_state`
nécessaires — la combinaison fragile `langgraph`/`langchain-openai`/`openai`
documentée plus haut pour le streaming n'a donc pas été touchée.

- **Téléchargement du modèle d'embeddings** (`sentence-transformers`) :
  aucun test n'a pu être exécuté avec un accès réseau à `huggingface.co`
  dans l'environnement de développement utilisé. La logique Qdrant est
  couverte avec un embedder factice déterministe (voir section Tests), mais
  `SentenceTransformer.encode()` en conditions réelles n'a pas été exercé.
- **Spawn réel de conteneurs Docker par `mcp-client`** : couvert avec un vrai
  serveur MCP lancé en process Python direct (même protocole que les vrais
  serveurs), mais pas avec le socket Docker ni les images `mcp/*` réelles.
- **`llama-server` : build, démarrage et inférence texte vérifiés
  réellement** (modèle `Qwen3.6-35B-A3B` quant `Q5_K_M` + `mmproj-F16`,
  conversation complète de bout en bout à travers `langgraph-agent`, voir
  section Backend d'inférence et tableau des bugs). **Non vérifié : function
  calling réel avec un tool_call effectif** (les tests d'intégration
  couvrant `has_tool_calls`/`require_approval`/`call_tools` restent basés
  sur des réponses LLM simulées, voir section Tests) **et le décodage WebP
  natif en conditions réelles** (`IMAGE_FORMAT_PASSTHROUGH=webp` — testé
  uniquement en conversation texte pure, jamais avec un `screen_shot`
  GhostDesk réel) ; aucun test de charge non plus.

