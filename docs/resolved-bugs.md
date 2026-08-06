# Bugs trouvés et corrigés pendant le développement

Chaque service a été exécuté réellement (pas seulement relu) avant livraison. Cette démarche a permis de trouver et corriger les bugs suivants — une section par bug (contenu identique au tableau précédent, restructuré pour rester diffable : voir docs/briefs/restructuration-et-anglais.md, phase 3).

### 1. `mcp-terminal` — `git` absent de l'image `python:3.12-slim` → `git_status` aurait planté

**Symptôme / cause confirmée** : `git` absent de l'image `python:3.12-slim` → `git_status` aurait planté

**Correctif** : `git` ajouté au `Dockerfile`

### 2. `mcp-terminal` — `shlex.quote` cassait `cat` sur les noms de fichiers avec espace (quoting shell inutile…

**Symptôme / cause confirmée** : `shlex.quote` cassait `cat` sur les noms de fichiers avec espace (quoting shell inutile en mode liste `subprocess`)

**Correctif** : remplacé par une résolution de chemin réelle (`os.path.realpath`) qui bloque aussi mieux le path traversal

### 3. `context-manager` — crash au démarrage si Qdrant pas encore prêt (`depends_on` sans condition ne garantit que…

**Symptôme / cause confirmée** : crash au démarrage si Qdrant pas encore prêt (`depends_on` sans condition ne garantit que l'ordre de démarrage des conteneurs)

**Correctif** : retry avec backoff au démarrage + `healthcheck` Qdrant dans le compose

### 4. `langgraph-agent` — double comptage de certains messages (contexte RAG, résultats d'outils) : les nœuds…

**Symptôme / cause confirmée** : double comptage de certains messages (contexte RAG, résultats d'outils) : les nœuds mutaient `state["messages"]` en place et retournaient l'état entier, ce qui perturbe le reducer `add_messages` de LangGraph

**Correctif** : chaque nœud retourne désormais uniquement son delta (`{"messages": [...]}`)

### 5. `langgraph-agent` — `InvalidUpdateError` de LangGraph quand un nœud ne retourne rien de neuf (`{}`)

**Symptôme / cause confirmée** : `InvalidUpdateError` de LangGraph quand un nœud ne retourne rien de neuf (`{}`)

**Correctif** : retour explicite `{"messages": []}`

### 6. `langgraph-agent` — `requirements.txt` ne pinnait pas `openai` : `langchain-openai==0.2.2` autorise…

**Symptôme / cause confirmée** : `requirements.txt` ne pinnait pas `openai` : `langchain-openai==0.2.2` autorise `openai<2.0.0,>=1.40.0`, mais les versions récentes d'`openai` (1.109+, 2.x) cassent le wrapper HTTP interne de `langchain-openai` (`AttributeError: 'AsyncHttpxClientWrapper' object has no attribute 'build_request'`) — un bug connu et récurrent entre les deux librairies (cf. [langchain-ai/langchain#19116](https://github.com/langchain-ai/langchain/issues/19116))

**Correctif** : `openai==1.51.2` épinglé explicitement, combinaison testée et validée

### 7. `mcp-client` — `requirements.txt` non installable tel quel : `pydantic==2.9.2` entrait en conflit avec…

**Symptôme / cause confirmée** : `requirements.txt` non installable tel quel : `pydantic==2.9.2` entrait en conflit avec `mcp==1.2.0`, qui exige `pydantic>=2.10.1` — `pip install` (donc le build Docker) aurait échoué

**Correctif** : `pydantic==2.10.3`

### 8. `langgraph-agent` — l'ajout du checkpointer pour la supervision humaine a introduit une duplication de…

**Symptôme / cause confirmée** : l'ajout du checkpointer pour la supervision humaine a introduit une duplication de l'historique : Open WebUI renvoie l'historique complet à chaque requête, mais celui-ci était désormais aussi persisté par thread — chaque tour réinjectait donc tout l'historique déjà stocké (2 tours simples produisaient 6 messages internes au lieu de 4)

**Correctif** : `owui_message_count` dans l'état du graphe : seuls les messages Open WebUI non encore vus sont soumis à chaque tour

### 9. `langgraph-agent` — avec Ollama (modèles Qwen3+) comme backend, le raisonnement du modèle est renvoyé dans un…

**Symptôme / cause confirmée** : avec Ollama (modèles Qwen3+) comme backend, le raisonnement du modèle est renvoyé dans un champ `reasoning` séparé de `content` sur les deltas SSE — hors format OpenAI standard, donc silencieusement ignoré par `langchain-openai` (`_convert_delta_to_message_chunk` ne lit que `content`/`tool_calls`/`function_call`) : la pensée du modèle n'atteignait jamais Open WebUI

**Correctif** : patch de `_convert_delta_to_message_chunk` (`app/graph.py`) qui replie `reasoning` dans `content`, entouré de `<think>...</think>` (convention reconnue par Open WebUI pour la bulle de pensée repliable) — appliqué en direct dans le flux de streaming, pas seulement en fin de réponse

### 10. `langgraph-agent` — le LLM n'était jamais lié aux outils MCP (`ChatOpenAI` instancié sans `bind_tools`) : le…

**Symptôme / cause confirmée** : le LLM n'était jamais lié aux outils MCP (`ChatOpenAI` instancié sans `bind_tools`) : le modèle ignorait purement et simplement l'existence de `terminal`/`filesystem`/`git`/`browser`/`desktop`(GhostDesk) et ne produisait donc jamais de `tool_calls` en usage réel — `require_approval`/`call_tools` restaient du code mort, alors que les 14 tests existants passaient quand même (ils simulent directement une réponse LLM avec `tool_calls` tout fait)

**Correctif** : `mcp-client` expose désormais `GET /tools/schema` (description + `inputSchema` de chaque outil, jusque-là jetés) ; `langgraph-agent` les récupère et les lie via `bind_tools` (`_get_bound_llm`, mis en cache pour la durée du process)

### 11. `langgraph-agent` — le résultat brut d'un outil (ex. `screen_shot` de GhostDesk, bloc image MCP `{"type":…

**Symptôme / cause confirmée** : le résultat brut d'un outil (ex. `screen_shot` de GhostDesk, bloc image MCP `{"type": "image", "data": <base64>, "mimeType": ...}`) était `json.dumps()` intégralement dans un `ToolMessage` — un rôle qui ne supporte que du texte au format OpenAI-compatible : le modèle recevait un blob base64 illisible, jamais une vraie image, indépendamment de ses capacités vision

**Correctif** : `_split_image_blocks` extrait les blocs image et les réinjecte en message `user` multimodal (`image_url`), seul rôle qui les supporte

### 12. `langgraph-agent` — même après le correctif ci-dessus, l'image restait invisible pour le modèle : le décodeur…

**Symptôme / cause confirmée** : même après le correctif ci-dessus, l'image restait invisible pour le modèle : le décodeur d'image d'Ollama (`mtmd`/llama.cpp) rejette explicitement le WebP (`"Failed to load image or audio file"`), format par défaut de `screen_shot`

**Correctif** : `_to_png_data_uri` (Pillow) reconvertit systématiquement en PNG avant transmission, plutôt que de compter sur le modèle pour penser à demander `format="png"` à chaque appel

### 13. `ollama` (service) — avec une image dans le contexte, le nombre de tokens (texte + tokens visuels) dépassait…

**Symptôme / cause confirmée** : avec une image dans le contexte, le nombre de tokens (texte + tokens visuels) dépassait le contexte par défaut choisi automatiquement par Ollama selon la VRAM disponible (4096 tokens observés) — `"request (4713 tokens) exceeds the available context size (4096 tokens)"`

**Correctif** : `OLLAMA_CONTEXT_LENGTH=16384` fixé explicitement dans `docker-compose.yml`

### 14. `mcp-client` — les clics souris GhostDesk (`mouse_click`, etc.) atterrissaient systématiquement à côté…

**Symptôme / cause confirmée** : les clics souris GhostDesk (`mouse_click`, etc.) atterrissaient systématiquement à côté de leur cible avec les modèles Qwen : ceux-ci raisonnent nativement en repère de coordonnées normalisé 0-1000, alors que GhostDesk interprète par défaut les coordonnées reçues comme des pixels écran natifs (documenté par GhostDesk)

**Correctif** : en-tête `GhostDesk-Model-Space` (`GHOSTDESK_MODEL_SPACE`, défaut `1000`) ajouté à chaque appel HTTP vers GhostDesk dans `_run_on_server`

### 15. `langgraph-agent` — avec `AUTO_APPROVED_TOOLS`, `call_llm` peut s'exécuter plusieurs fois d'affilée sans…

**Symptôme / cause confirmée** : avec `AUTO_APPROVED_TOOLS`, `call_llm` peut s'exécuter plusieurs fois d'affilée sans pause d'approbation (boucle capture/clic GhostDesk) ; chaque appel remettait l'état de la balise `<think>` à zéro, donc chaque itération de raisonnement rouvrait sa propre balise en plein milieu du flux — Open WebUI n'affiche en bulle repliable que celle en tout début de message, les suivantes apparaissaient en texte brut visible (ex. observé en usage réel : `<think>...<think>...</think>Cliqué.`)

**Correctif** : état `think_opened`/`think_closed` déplacé de la variable de contexte locale à `AgentState` (comme `tool_iterations`), reporté d'un appel de `call_llm` à l'autre au sein d'un même tour et remis à `False` uniquement au tout début d'un nouveau tour (`_resolve_run`, `app/main.py`) — un seul bloc `<think>` continu sur toute la boucle

### 16. `langgraph-agent` — `tool_iterations` ne se réinitialise jamais entre deux tours "approuver" (seulement sur…

**Symptôme / cause confirmée** : `tool_iterations` ne se réinitialise jamais entre deux tours "approuver" (seulement sur un tout nouveau message utilisateur) : le budget de `MAX_TOOL_ITERATIONS` (5 à l'origine) est donc partagé sur toute une chaîne d'approbations, épuisé en 2-3 aller-retours à peine, avant même la boucle GhostDesk auto-approuvée qui en consomme 2 par geste (capture+clic) — `has_tool_calls` force alors la fin du graphe MÊME SI le dernier message du modèle contient un tool_calls en attente, silencieusement jeté sans aucun message d'explication (observé en usage réel : l'agent semblait "s'arrêter" en plein milieu d'une tâche, ex. en train de taper une URL)

**Correctif** : `MAX_TOOL_ITERATIONS` relevé (configurable via env, défaut `20`) ; `app/main.py` détecte désormais ce cas (dernier message avec `tool_calls` mais graphe non mis en pause) et renvoie une notice explicite au lieu du texte de raisonnement brut ; `recursion_limit` de LangGraph (25 par défaut, indépendant de `MAX_TOOL_ITERATIONS` et bien plus vite atteint par une longue boucle auto-approuvée) relevé en conséquence pour éviter un `GraphRecursionError` brut avant même d'atteindre cette notice

### 17. `ollama` (modèle `agent-llm`, quant IQ2_M) — un tour de raisonnement pouvait dégénérer en dérive sémantique (pas une répétition mot à…

**Symptôme / cause confirmée** : un tour de raisonnement pouvait dégénérer en dérive sémantique (pas une répétition mot à mot, mais une cascade de synonymes de plus en plus rares/incohérents, ex. observé en usage réel sur la tâche "va sur google.fr" : dérive vers une énumération de gentilés régionaux français puis d'ères géologiques) sans jamais produire de `tool_calls`, jusqu'à saturer tout le contexte (`OLLAMA_CONTEXT_LENGTH`). Nos garde-fous (`MAX_TOOL_ITERATIONS`/`AUTO_APPROVAL_STREAK_LIMIT`) ne s'appliquent pas ici : ils comptent des itérations d'*outils*, pas la longueur d'une génération. Cause réelle, confirmée en comparant l'horodatage du manifest Ollama (recréation à 10:56) à celui de la conversation cassée (11:12) puis en rejouant la même tâche après correction : le Modelfile de `agent-llm` avait été durci un peu plus tôt dans la même session (`repeat_penalty` `1.0`→`1.15`, `repeat_last_n` `64`→`1024`, `presence_penalty` déjà à `1.5`) pour parer une boucle de répétition redoutée, mais cette combinaison était en réalité bien trop agressive pour un modèle aussi quantisé — en interdisant la réutilisation de mots sur une fenêtre de 1024 tokens, elle forçait le modèle à piocher un vocabulaire toujours plus rare pour continuer, provoquant elle-même la dérive observée. Une première explication écrite ici ("`repeat_last_n` trop court") s'est donc révélée fausse : le réglage durci était déjà actif *pendant* la dérive, pas absent

**Correctif** : Modelfile assoupli : `repeat_penalty` `1.15`→`1.05`, `repeat_last_n` `1024`→`256`, `presence_penalty` `1.5`→`0` — revérifié en rejouant "va sur google.fr" via `/v1/chat/completions`, deux tours consécutifs cohérents (`key_type` puis `key_press`, sans dérive). Ce réglage vivait uniquement dans le store Ollama du conteneur (volume `ollama-data`), perdu au moindre `ollama pull`/`cp` refait à la main : `scripts/rebuild-agent-llm.sh <modèle-source>` fige désormais la recette dans le repo pour la réappliquer à l'identique quel que soit le modèle source, y compris après un changement de modèle puis un retour au modèle actuel. `LLM_MAX_TOKENS` (configurable via env, défaut `2048`, `app/graph.py`) conservé en filet de sécurité indépendant, pour plafonner tout dérapage résiduel d'un tour plutôt que de laisser saturer tout le contexte

### 18. `llama-server` — build CUDA échouant à l'édition de liens (`undefined reference to…

**Symptôme / cause confirmée** : build CUDA échouant à l'édition de liens (`undefined reference to cuMemCreate/cuDeviceGet/cuGetErrorString/...`) : ggml active par défaut l'allocateur "CUDA Virtual Memory Management" (pooling KV-cache), qui lie `ggml-cuda` contre le driver CUDA réel (`libcuda.so`, cible CMake `CUDA::cuda_driver`) — absent d'une image `*-devel` au moment du build (fourni seulement au runtime par le driver hôte via `nvidia-container-toolkit`, jamais pendant un `docker build` classique)

**Correctif** : `-DGGML_CUDA_NO_VMM=ON` ajouté à la configuration CMake — ne touche ni `--flash-attn` ni `--cache-type-v turbo3`, seulement cet allocateur de pooling (peu pertinent ici avec `--parallel 1`)

### 19. `llama-server` — Blackwell (sm_120, RTX 5060 Ti) non pris en charge : la base `nvidia/cuda:12.4.1-*`…

**Symptôme / cause confirmée** : Blackwell (sm_120, RTX 5060 Ti) non pris en charge : la base `nvidia/cuda:12.4.1-*` initialement choisie ne supporte pas la compilation pour cette architecture (confirmé dans le CMakeLists du fork : `120a-real` nécessite CUDA >= 12.8)

**Correctif** : base `nvidia/cuda:12.8.1-devel/runtime-ubuntu22.04` + `CMAKE_CUDA_ARCHITECTURES="89-real;120a-real"` explicite (Ada + Blackwell) plutôt que la détection "native" (nécessite un GPU visible PENDANT le build, absent d'un `docker build` standard) — revérifié via `llama-server --list-devices --gpus all`, les deux GPU détectés

### 20. `llama-server` — binaire buildé mais inexécutable :…

**Symptôme / cause confirmée** : binaire buildé mais inexécutable : `libllama-common.so.0`/`libmtmd.so.0`/`libllama.so.0`/`libggml-base.so.0` introuvables au lancement (`cannot open shared object file`) — le build CMake de ce fork produit les bibliothèques partagées dans le même dossier que les exécutables, mais SANS RPATH/RUNPATH embarqué (vérifié via `readelf -d`), contrairement à l'hypothèse initiale d'une résolution `$ORIGIN` ; `libgomp.so.1` (OpenMP, utilisé par le backend CPU de ggml) manquait aussi de l'image runtime

**Correctif** : `COPY --from=build /src/build/bin/ /app/` (tout le dossier, pas seulement le binaire) + `ENV LD_LIBRARY_PATH=/app` + `libgomp1` ajouté aux paquets runtime

### 21. `llama-server` — conteneur en boucle de redémarrage au premier lancement réel : `--flash-attn` (passé sans…

**Symptôme / cause confirmée** : conteneur en boucle de redémarrage au premier lancement réel : `--flash-attn` (passé sans valeur dans `entrypoint.sh`, comme un simple flag booléen) avalait l'argument suivant (`--jinja`) comme sa propre valeur — `error: unknown value for --flash-attn: '--jinja'`. Ce fork a changé `-fa`/`--flash-attn` d'un flag booléen vers une option à valeur obligatoire (`on`/`off`/`auto`), confirmé via `llama-server --help`

**Correctif** : `--flash-attn on` explicite dans `entrypoint.sh` — revérifié en relançant le conteneur, plus de boucle de redémarrage, modèle chargé jusqu'au bout

### 22. `langgraph-agent` — avec llama-server (fork turboquant-webp) comme backend, le raisonnement du modèle…

**Symptôme / cause confirmée** : avec llama-server (fork turboquant-webp) comme backend, le raisonnement du modèle disparaissait silencieusement du flux streamé (aucune erreur, juste absent) — le patch `_convert_delta_with_reasoning` (`app/graph.py`) ne lisait que le champ `reasoning` (convention Ollama, sur laquelle il avait été écrit et testé), alors que llama-server streame le raisonnement dans un champ `reasoning_content` (convention DeepSeek-R1/OpenAI o1). Confirmé en inspectant les deltas SSE bruts d'un vrai appel streamé contre le vrai binaire : jamais de clé `reasoning`, toujours `reasoning_content`

**Correctif** : le patch lit désormais `reasoning` OU `reasoning_content` (`_dict.get("reasoning") or _dict.get("reasoning_content")`) — revérifié de bout en bout via `langgraph-agent` réel : `<think>` s'ouvre, le raisonnement s'affiche, `</think>` se ferme avant la réponse finale, comme avec Ollama

### 23. `langgraph-agent` — observé en usage réel (conversation "va sur wikipedia.org et cherche l'article sur la…

**Symptôme / cause confirmée** : observé en usage réel (conversation "va sur wikipedia.org et cherche l'article sur la ville de toulouse, en français", pilotage GhostDesk) : le modèle finissait parfois un tour SANS aucun `tool_calls` structuré ET sans texte de réponse visible — sa tentative d'appel d'outil restait écrite en prose façon Qwen (`<tool_call><function=NOM><parameter=...>`) noyée dans le raisonnement (`reasoning_content`), jamais reconnue comme un vrai tool_calls OpenAI. **Cause racine confirmée** en lisant le parseur du fork (`common/chat-auto-parser-generator.cpp`) : le raisonnement (`<think>...`) est capturé comme texte LIBRE, non contraint par la grammaire, jusqu'à rencontrer `</think>` — la grammaire stricte du tool-calling n'est appliquée qu'APRÈS cette balise. Si le modèle "tente" un appel avant d'avoir fermé `</think>` (observé après un raisonnement anormalement long/répétitif, à rapprocher de la dérive sémantique déjà documentée pour Ollama), la tentative reste piégée dans la zone non contrainte. Confirmé non-déterministe (rejouer le même prompt donne tantôt un `tool_calls` correct, tantôt cet échec) et confirmé résolu par `/no_think` (contourne entièrement ce chemin de code, voir Thinking adaptatif) — mais celui-ci ne s'injecte qu'à partir du tour suivant un tour auto-approuvé, pas sur le tout premier tour d'une tâche, là où le bug a justement été observé la première fois. Sans correctif, l'utilisateur ne voyait que la bulle de raisonnement se refermer sur rien, exactement le symptôme "l'agent s'arrête en plein milieu d'une tâche" déjà documenté pour `MAX_TOOL_ITERATIONS`

**Correctif** : Trois mitigations complémentaires (aucune ne corrige la cause côté serveur/modèle, hors de portée ici) : **(1)** `_extract_fallback_tool_call` (`app/graph.py`) reconnaît la syntaxe `<tool_call><function=...>` piégée dans le texte et la reconstruit en tool_calls structuré avant même de compter le tour comme un échec (log `WARNING` à chaque récupération, pour garder la visibilité sur la fréquence réelle du problème) ; **(2)** `retry_empty_answer` reboucle automatiquement sur `call_llm` jusqu'à `MAX_EMPTY_ANSWER_RETRIES` fois (défaut `1`, budget cumulé pour toute la tâche comme `tool_iterations`) quand la reconstruction échoue aussi ; **(3)** au-delà, `has_visible_answer`/`_format_empty_answer_notice` (`app/main.py`) affiche une notice explicite plutôt qu'un message vide. **Confirmé efficace en conditions réelles** : sur 4 tâches indépendantes rejouées après déploiement du correctif, le parseur de secours s'est déclenché 5 fois (`app_launch`, `app_running` ×2, `screen_shot`) et a récupéré l'intention du modèle à chaque fois, sans qu'aucune des 4 tâches n'affiche la notice de repli

### 24. `ocr-service` — build de l'image réellement exécuté (jusque-là seule la suite de tests, en…

**Symptôme / cause confirmée** : build de l'image réellement exécuté (jusque-là seule la suite de tests, en `OCR_ENGINE=fake`, avait tourné) : trois échecs successifs. **(1)** `libgomp.so.1` introuvable — absent de `python:3.12-slim`, requis dès l'import de paddlepaddle (même classe de bug que `llama-server` ci-dessus). **(2)** une fois corrigé, `ModuleNotFoundError: No module named 'setuptools'` — `paddle.utils.cpp_extension` l'importe inconditionnellement dès `import paddle`, absent par défaut de cette image (seul `pip` y est préinstallé). **(3)** une fois les deux corrigés, crash restant (`Segmentation fault`, puis `double free or corruption`/`munmap_chunk(): invalid pointer` selon le run — symptôme différent à chaque fois selon l'ASLR, signature classique d'une corruption de tas plutôt que d'une dépendance manquante). **Cause racine confirmée par backtrace `gdb`** : `paddlepaddle==2.6.2` embarque sa propre copie de `zlib` dans `libpaddle.so`, avec des symboles globaux non isolés (`inflateReset2`) qui entrent en collision avec `libz.so.1` système dès que `Cython` (importé en cascade par `paddle.utils.cpp_extension`) décompresse quoi que ce soit via le module `zlib` de la stdlib

**Correctif** : `libgomp1`/`libgl1`/`libglib2.0-0` (ce dernier duo requis par `cv2`, dépendance transitive de paddleocr) ajoutés au `Dockerfile` ; `setuptools==75.6.0` épinglé dans `requirements.txt` ; `ENV LD_PRELOAD=/lib/x86_64-linux-gnu/libz.so.1` force la résolution vers la bonne bibliothèque, au build ET à l'exécution (import paresseux dans `app/ocr_engine.py`) — revérifié en buildant l'image pour de vrai (téléchargement des modèles PP-OCRv3 au build) puis en déclenchant un vrai appel `read_screen` via `mcp-client` contre GhostDesk : texte réel détecté à l'écran avec coordonnées et scores de confiance

### 25. `llama-server` — découvert en rejouant le harnais de baseline Phase 0 de la migration…

**Symptôme / cause confirmée** : découvert en rejouant le harnais de baseline Phase 0 de la migration langgraph/langchain-openai/openai (`tests_integration/test_tool_calling_baseline.py`) : une bonne partie des tours (jusqu'à environ la moitié sur une session de 25 générations réelles) se terminaient par la notice de repli `⚠️ Erreur interne pendant la génération, réessayez.` (`except Exception` de `_stream_response`, `app/main.py`) au lieu d'un résultat de tool-calling exploitable — crash GPU dur (`CUDA error`, `device 1`/Blackwell) capturé dans les logs `llama-server`, qui se relance seul après coup (superviseur interne), d'où l'absence de panne totale visible côté utilisateur. **Cause confirmée** par une matrice d'expériences dédiée (`tests_integration/CUDA-DIAGNOSTIC.md`, historique complet) : chemin de copie/synchronisation **inter-GPU** du fork sous gros lot de prefill (`--ubatch-size` par défaut 512), sur `--tensor-split` hétérogène (Ada + Blackwell) — confirmé par une paire de runs mono-GPU stables en isolation (Blackwell seule et Ada seule, aucun crash sous charge équivalente) et par un `Xid 31` (MMU Fault, moteur copy-engine `CE4`) au `dmesg`, signature logicielle plutôt que matérielle. Alimentation/matériel explicitement exonérés (recâblage et plafonnement de puissance sans effet)

**Correctif** : **Contournement adopté en production** : `--ubatch-size 128` (au lieu du défaut 512), désormais permanent dans `entrypoint.sh` — revérifié par un run de validation complet (25/25 générations réussies, prompt set avec images, 0 crash). Coût mesuré : environ **+34 % de latence par génération réussie** (~13-17 s au lieu de ~13 s, chiffrage détaillé et biais de mesure discutés dans `CUDA-DIAGNOSTIC.md`) — compensé par la disparition des tours en échec complet (~40-50 % avant correctif). Seuil encadré : **stable à ≤256** (75 générations cumulées sans crash entre 128 et 256), **crash confirmé à 512** — `256` écarté malgré sa stabilité, pas de gain net justifiant de réduire la marge de sécurité vs 128. **Statut : issue amont en préparation** (bug probablement upstream `ggml-org/llama.cpp`, repro vanilla en cours pour router l'issue au bon dépôt)

### 26. `mcp-client` — découvert en testant une tâche de navigation web réelle multi-étapes :…

**Symptôme / cause confirmée** : découvert en testant une tâche de navigation web réelle multi-étapes : `filesystem`/`git`/`browser`(playwright)/`terminal` (les 4 serveurs MCP spawnés via `docker run` en sous-processus, contrairement à `desktop`/`ocr` en HTTP persistant) échouaient silencieusement à l'enregistrement — `_refresh_registry()` avale toute exception (`except Exception: continue`) sans logger la cause, donc `/tools/schema` ne renvoyait que les 16 outils GhostDesk/OCR au lieu des 63 attendus, sans aucune erreur visible. **Cause racine confirmée** : le binaire client `docker` était absent du conteneur (`which docker` échouait) malgré une installation apt du paquet `docker.io` sans erreur au build — ce paquet, sur le dépôt Debian trixie utilisé par l'image `python:3.12-slim`, installe seulement le démon (`dockerd`) et `docker-proxy`, **pas** le binaire client `/usr/bin/docker` lui-même (découpage de paquet incomplet)

**Correctif** : `Dockerfile` : remplacement de l'installation apt par une copie directe du binaire depuis l'image officielle `docker:27-cli` (`COPY --from=docker:27-cli /usr/local/bin/docker ...`) — revérifié via `docker --version` dans le conteneur puis un vrai appel `GET /tools/schema` : 63 outils listés (dont tout le jeu `browser_*` de Playwright), contre 16 avant. Penser à redémarrer `langgraph-agent` après ce correctif : `_tools_schema_cache` (`app/graph.py`) est mis en cache pour la durée du process et ne se rafraîchit jamais tout seul

### 27. `mcp-client` (serveur "browser"/Playwright) — Contrairement à `desktop`(GhostDesk)/`ocr`, HTTP persistants, le serveur "browser" était…

**Symptôme / cause confirmée** : **résolu.** Contrairement à `desktop`(GhostDesk)/`ocr`, HTTP persistants, le serveur "browser" était spawné de façon éphémère (`docker run -i --rm mcp/playwright:latest`) à **chaque appel d'outil** — sans continuité de session entre deux appels. Découvert en forçant une tâche de navigation Wikipédia à plusieurs étapes via `browser_navigate` puis `browser_snapshot` : le second appel démarrait un navigateur tout neuf (`about:blank`), sans mémoire de la page visée par le premier. Le modèle s'en sortait par une dégradation propre (repli sur le titre déjà présent dans la réponse texte de `browser_navigate`), mais toute tâche multi-étapes nécessitant un vrai état partagé (naviguer puis cliquer, remplir un formulaire sur plusieurs appels...) échouait silencieusement de la même façon

**Correctif** : Deux correctifs cumulatifs, tous deux vérifiés par un vrai appel `browser_navigate` suivi d'un `browser_snapshot` séparé (le second retourne la page visitée par le premier, pas `about:blank`) : (1) `docker-compose.yml` — nouveau service `playwright-mcp` : l'image officielle `mcp/playwright` supporte un mode serveur HTTP natif (`--host 0.0.0.0 --port 8931`, endpoint Streamable HTTP `/mcp`, vérifié en le lançant), remplace le spawn éphémère ; (2) même une fois le PROCESS serveur persistant, `mcp-client` ouvrait encore une SESSION MCP neuve à chaque appel (`_run_on_server`) — et Playwright MCP scope son contexte navigateur (page, cookies, historique) à la session, pas au process : `about:blank` réapparaissait donc identiquement. Ajout de `_get_persistent_session`/`_persistent_sessions` dans `app/main.py` : la session "browser" reste ouverte entre deux appels HTTP au lieu d'être fermée à chaque fois (les autres serveurs http, dont l'état vit hors session MCP, gardent le comportement éphémère). Effet de bord corrigé au passage : `_run_on_server` construisait `Authorization: Bearer {token}` même quand `token=""`, et `httpx` rejette l'en-tête résultant (`Bearer ` avec espace final) comme illégal — l'en-tête n'est plus ajouté quand le token est vide

### 28. `mcp-client`/`docker-compose` (volume `agent-downloads`) — Phase 1d-révisée (voir docs/history.md, T5) : le fichier téléchargé par playwright-mcp…

**Symptôme / cause confirmée** : Phase 1d-révisée (voir docs/history.md, T5) : le fichier téléchargé par playwright-mcp existait bien sur disque, mais `read_file` échouait en `ENOENT` côté serveur filesystem — les deux services référençaient en réalité DEUX volumes Docker différents. `docker-compose.yml` résout `agent-downloads` en `agentic-ai-playground_agent-downloads` (préfixe de projet), mais `mcp-client` spawne le serveur filesystem via un `docker run` BRUT sur le socket hôte (extérieur au fichier compose) : Docker n'y applique aucun préfixe, "agent-downloads" y désignait un volume totalement différent (vide, créé à la volée)

**Correctif** : `name: agent-downloads` fixé explicitement dans `docker-compose.yml` (supprime toute ambiguïté de préfixage) — revérifié en écrivant un fichier via playwright-mcp puis en le lisant via `read_file` du serveur filesystem

### 29. `playwright-mcp` (volume `agent-downloads`) — Même chantier : une fois le bug de volume ci-dessus corrigé, `browser_navigate` échouait…

**Symptôme / cause confirmée** : Même chantier : une fois le bug de volume ci-dessus corrigé, `browser_navigate` échouait systématiquement en `Error: EACCES: permission denied, open '/downloads/page-...yml'` — un volume Docker nommé est créé `root:root` par défaut, l'image `mcp/playwright` tourne en utilisateur `node` (uid 1000), qui ne pouvait donc pas y écrire son propre snapshot de debug sous `--output-dir`. Découvert directement grâce à la persistance du résultat d'outil dans le journal d'audit (`entry["result"]`) — sans elle, ce bug serait resté invisible

**Correctif** : Conteneur d'initialisation dédié `agent-downloads-init` (image `busybox`, `chown -R 1000:1000 /downloads`), exécuté une fois avant `playwright-mcp` via `depends_on: condition: service_completed_successfully`

### 30. `mcp-client`/`playwright-mcp` (session "browser" partagée) — Phase 1d-révisée (voir docs/history.md, correctif extraction) : un T7×5 à threads indépendants…

**Symptôme / cause confirmée** : Phase 1d-révisée (voir docs/history.md, correctif extraction) : un T7×5 à threads indépendants donnait 0/5, détail et tool_calls_observés STRICTEMENT identiques sur les 5 répétitions — signe que le modèle rejouait depuis un état qu'il ne devrait pas avoir. Chaque snapshot montrait un onglet fantôme `[Science \| Books to Scrape - Sandbox]` (résidu d'une tâche T10 complètement différente, exécutée des heures plus tôt) : la session Playwright persistante (`persistent_session: True`) est PARTAGÉE par tout mcp-client, jamais scopée par thread langgraph-agent ni par tâche — rien ne fermait les onglets entre deux tâches, seul un redémarrage complet de `playwright-mcp` purgeait cet état

**Correctif** : `POST /reset-session/{server_name}` (`services/mcp-client/app/main.py`) : jette la session persistante en cache (`_drop_persistent_session`), le prochain appel en rouvre une neuve. Appelé par le harnais de tâches web avant CHAQUE répétition (`_reset_browser_session`, `tests_integration/test_web_tasks.py`) — revérifié par un T7×5 propre (1/5, contamination confirmée écartée bien que non seule responsable du recul T7)

### 31. `langgraph-agent` (`_tools_schema_cache`) — Même chantier : une première campagne complète avec `browser_extract` fraîchement déployé…

**Symptôme / cause confirmée** : Même chantier : une première campagne complète avec `browser_extract` fraîchement déployé donnait un résultat incohérent avec l'hypothèse testée (T1 toujours 0/3). Vérifié via `POST /context` (`tools_schema.count`) : le schéma vu par le thread ne comptait que 63 outils alors que mcp-client en servait déjà 64 — `_tools_schema_cache` (`app/graph.py`) est un cache PROCESS-LIFETIME côté langgraph-agent (rempli une fois, jamais invalidé), qu'un redémarrage de mcp-client seul (fait pour isoler une autre variable) ne suffit pas à rafraîchir si langgraph-agent, lui, n'a pas redémarré depuis. `browser_extract` n'avait donc jamais été réellement proposé au modèle durant ce run, l'invalidant

**Correctif** : Redémarrage de `langgraph-agent` (`docker compose restart langgraph-agent`) — pas un changement de code, mais une fragilité opérationnelle à retenir : tout changement du schéma d'outils exposé par mcp-client exige aussi un redémarrage de langgraph-agent, pas seulement du service modifié. Revérifié via `POST /context` : 64 outils après redémarrage

### 32. `langgraph-agent` (appels LLM auxiliaires du pipeline « cœur cognitif », Itération 3) — Découvert en faisant réellement tourner la campagne live de l'Itération 3 (stack et GPU…

**Symptôme / cause confirmée** : Découvert en faisant réellement tourner la campagne live de l'Itération 3 (stack et GPU disponibles) : `plan_task`/`verify_action`/`replan_task`/`revise_plan`/`_judge_plan` retombaient systématiquement sur leur repli d'erreur en conditions réelles, jamais sur une vraie évaluation. Confirmé par un appel direct à TabbyAPI (contournant langgraph-agent) : Qwen3.6 raisonne dans un champ `reasoning_content` SÉPARÉ de `content` avant de répondre — ce raisonnement, souvent long (plusieurs milliers de tokens), consommait à lui seul tout `LLM_MAX_TOKENS` (2048, dimensionné pour la boucle conversationnelle principale, partagé à tort par ces 5 appels auxiliaires), tronquant `content` à vide ou en plein milieu du JSON attendu (`finish_reason="length"`). `/no_think` en préfixe de prompt (mécanisme `ADAPTIVE_THINKING` existant) ne supprime PAS ce raisonnement sur ce backend (vérifié par le même appel direct)

**Correctif** : Nouveau client `planner_llm` (`app/graph.py`), séparé de `llm`, avec son propre budget `PLANNER_MAX_TOKENS` (défaut `8192`) — `llm`/`LLM_MAX_TOKENS` (2048) reste inchangé, toujours le filet de sécurité voulu contre les dérives de répétition de la boucle principale. Revérifié par un appel direct à TabbyAPI avec `max_tokens=6000` : réponse JSON complète, `finish_reason="stop"`

### 33. `langgraph-agent` (planificateur, Itération 3) — Même campagne live : le planificateur déclarait systématiquement des outils inventés mais…

**Symptôme / cause confirmée** : Même campagne live : le planificateur déclarait systématiquement des outils inventés mais inexistants (`web_browser`, `search`, `extract_text`...), rejetés à chaque fois par l'heuristique "outils référencés existants" (`app/plan_validation.py`) — aucun plan ne passait jamais la validation, quelle que soit la qualité de la décomposition elle-même

**Correctif** : `_available_tools_hint()` (`app/graph.py`) : ajoute la liste réelle des noms d'outils MCP (`_get_tools_schema()`) au message UTILISATEUR envoyé au planificateur (pas au system prompt, pour rester à jour si le schéma change entre deux tâches) — utilisée par `plan_task`/`revise_plan`/`replan_task`. Revérifié en conditions réelles : plan généré avec des noms d'outils réels (`browser_navigate`, `browser_extract`...), heuristiques passées

### 34. `langgraph-agent` (`POST /approve`, Itération 3) — Même campagne live, après le correctif de `_resolve_run` pour distinguer une pause…

**Symptôme / cause confirmée** : Même campagne live, après le correctif de `_resolve_run` pour distinguer une pause `require_plan_approval` (plan) d'une pause `require_approval` (outil) : `POST /approve` (utilisé par le bouton d'action Open WebUI, chemin distinct du message texte "approuver"/"refuser") mettait ENCORE inconditionnellement à jour `approved`/`grant_session`, jamais `plan_approved` — une pause de plan approuvée via ce bouton restait indéfiniment bloquée malgré une réponse 200 OK apparemment réussie

**Correctif** : Même distinction `"require_plan_approval" in snapshot.next` appliquée à `/approve` (`app/main.py`) qu'à `_resolve_run` — revérifié à la fois en conditions réelles (le bouton débloque désormais la tâche) et par un nouveau test HTTP dédié (`test_approve_endpoint_resumes_plan_approval_pause`)

### 35. `langgraph-agent` (`app/approval_policy.py`) — Trouvé par le préambule de campagne (Itération 0) lui-même, en le faisant tourner pour de…

**Symptôme / cause confirmée** : Trouvé par le préambule de campagne (Itération 0) lui-même, en le faisant tourner pour de vrai avant la sonde live de l'Itération 4 : `PreflightError` sur `git_branch` absent du schéma effectif. Vérifié via `GET /tools/schema` sur langgraph-agent ET mcp-client (64 outils, d'accord entre eux — pas de désynchronisation) : `_DEFAULT_TIER_READ` référençait `"git_branch"`, qui n'a jamais correspondu à un outil réel du serveur MCP git officiel (12 outils vérifiés en direct) — seul `git_create_branch` (déjà classé séparément en `_DEFAULT_TIER_REVERSIBLE`) gère les branches. Resté inoffensif en usage réel (un outil jamais proposé au modèle n'est jamais appelé) jusqu'à ce que `campaign_preflight.py` (Itération 0) commence à comparer les tiers déclarés au schéma réel

**Correctif** : Entrée `"git_branch"` retirée de `_DEFAULT_TIER_READ` — revérifié : le préambule de campagne passe désormais sans erreur

### 36. `langgraph-agent` (`verify_action`, Itération 4) — Sonde 1 (1/3) vs sonde 2 (`VERIFICATION_ENABLED=false`, 2/3, T1 réussit flag désactivé) :…

**Symptôme / cause confirmée** : Sonde 1 (1/3) vs sonde 2 (`VERIFICATION_ENABLED=false`, 2/3, T1 réussit flag désactivé) : isolé `verify_action` comme cause de l'échec T1. Il jugeait une sous-tâche « échouée » en se fiant littéralement à un `success_criterion` généré par le planificateur (ex. « utilise la barre de recherche »), sans jamais voir la page réelle — sur le site fixture catalogue, seule la pagination existe, l'agent progressait donc réellement mais était jugé en échec à répétition

**Correctif** : `_fetch_verification_snapshot(objective)` (`app/graph.py`) : capture un `browser_snapshot` frais après tout tour utilisant un outil `browser_*`, transmis au juge comme `etat_actuel_de_la_page` — prompt mis à jour pour juger la progression réelle, pas la lettre du critère. Revérifié par sonde 3 : T7 passe de échec à réussite

### 37. `langgraph-agent` (planificateur/juge de plan, Itération 4, suite) — Sonde 3 : T7 corrigé mais T1 échoue encore (plus lentement, 11 min). Log confirmé :…

**Symptôme / cause confirmée** : Sonde 3 : T7 corrigé mais T1 échoue encore (plus lentement, 11 min). Log confirmé : `verify_action` voyait bien l'absence de barre de recherche, mais `plan_task`/`revise_plan`/`replan_task`/`_judge_plan` ne voyaient JAMAIS le contenu réel de la page — ils continuaient d'exiger une recherche à chaque cycle de replanification, même défaut d'ancrage que `verify_action`, source différente

**Correctif** : `_grounding_snapshot(state, objective)` (réutilise `_fetch_verification_snapshot`), `None` si `current_page_url` est vide (le tout premier `plan_task` reste non ancré, aucune navigation n'a encore eu lieu). `revise_plan`/`replan_task`/`_judge_plan` reçoivent ce snapshot quand disponible. Revérifié par sonde 4 : T1 réussit enfin (prix trouvé)

### 38. `docker-compose.yml`/`.env` (Itération 4, opérationnel, pas un bug de code) — Entre la sonde 3 et la reconstruction pour la sonde 4, `.env` ne persistait que…

**Symptôme / cause confirmée** : Entre la sonde 3 et la reconstruction pour la sonde 4, `.env` ne persistait que `PLAN_VALIDATION_ENABLED`/`PLAN_JUDGE_ENABLED` — `PLANNER_ENABLED`/`VERIFICATION_ENABLED` avaient été activés sonde par sonde sans être écrits dans `.env`. `docker compose up -d --build langgraph-agent` a détecté une dérive de config et recréé plusieurs conteneurs (dont `tabbyapi`, rechargement du modèle), remettant silencieusement ces deux flags à leur défaut (`false`) — repéré en revérifiant les arguments dans le conteneur avant de relancer la sonde (règle mémoire « vérifier les arguments avant campagne »), pas après coup

**Correctif** : Les 4 flags ajoutés explicitement à `.env`, revérifiés via `docker exec langgraph-agent env` après reconstruction avant de lancer la sonde 4

### 39. `tests_integration/test_web_tasks.py` (`_run_campaign`/`_derive_thread_id`, PAS un bug du cœur cognitif lui-même) — Campagne finale Itération 4 (28/33) : T8_wikipedia a échoué 0/3, mais les 3 échecs…

**Symptôme / cause confirmée** : Campagne finale Itération 4 (28/33) : T8_wikipedia a échoué 0/3, mais les 3 échecs affichent EXACTEMENT le même nombre de tokens (`Prompt length 170285 exceeds... 32768`), et les répétitions #2/#3 échouent en 0.4s (contre 430.7s pour #1). `_derive_thread_id` (`app/main.py`) hache uniquement le texte du premier message humain — or les prompts de `TASKS` sont des constantes FIXES, identiques d'une répétition à l'autre au sein d'une même campagne (contrairement aux sondes de ce chantier, qui ajoutent un marqueur unique). Les 3 « répétitions » d'une même tâche dans `_run_campaign()` partagent donc le MÊME thread_id, donc le MÊME état persisté par le checkpointer (`MemorySaver`, en mémoire) : la répétition 1 a fait grimper le thread à 170285 tokens (grosse page Wikipedia réelle + plusieurs cycles de plan/replan/juge, Itération 4) et échoué AVANT toute sauvegarde de checkpoint (l'erreur `BadRequestError` survient pendant l'appel LLM, avant toute mutation d'état) ; les répétitions 2 et 3 rejouent alors le MÊME message humain sur un thread déjà bloqué au même point, ré-échouant identiquement et quasi instantanément, sans être des essais réellement indépendants. Bug latent probablement présent depuis l'origine du harnais, jamais manifesté avant l'Itération 4 : les campagnes précédentes restaient toujours confortablement sous 32768 tokens même cumulées sur 3 répétitions d'un même thread

**Correctif** : `_run_campaign()` (`tests_integration/test_web_tasks.py`) applique désormais le même correctif que `test_t7_noise_baseline`/`test_download_then_filesystem_read_roundtrip` (déjà en place, jamais étendu ici) : un marqueur unique (`uuid.uuid4().hex[:8]`) ajouté au prompt de CHAQUE répétition, garantissant un `thread_id` distinct — chaque répétition d'une campagne officielle est désormais un essai réellement indépendant. Consigné ici pour que le score 28/33 de la campagne finale soit interprété correctement : T8 y représentait RÉELLEMENT 1 échec de dépassement de contexte, pas 3 échecs indépendants (à re-mesurer avec le correctif pour une lecture propre)

### 40. `langgraph-agent` (`app/graph.py`, garde-fou anti-fabrication d'URL) — Correctif latence 1/2-ter/2-2 : campagne de checkpoint, T8 (0/3) et T11 (échecs) —…

**Symptôme / cause confirmée** : Correctif latence 1/2-ter/2-2 : campagne de checkpoint, T8 (0/3) et T11 (échecs) — diagnostiqué d'abord à tort comme panne d'infrastructure `playwright-mcp` (le raisonnement du modèle décrivait un « navigateur bloqué sur about:blank »), écarté par preuves (RestartCount=0, OOMKilled=false, mémoire stable, aucun événement `die`/`restart`/`oom`). Le vrai résultat d'outil, lui, montrait le message de refus de NOTRE garde-fou anti-fabrication (« URL non observée sur cette page ») : `_task_scope_urls` suppose qu'une tâche mentionne toujours l'URL cible dans son prompt, mais T8 (« sur Wikipédia... ») et T11 (« quelle est la dernière version de Python ? ») n'en mentionnent aucune — leur toute PREMIÈRE navigation, pourtant légitime, était donc systématiquement refusée comme une fabrication

**Correctif** : `has_prior_navigation` (`_execute_tool_calls`) : la toute première navigation d'une tâche est désormais toujours autorisée (rien n'a encore été observé, aucune fabrication possible sur un premier choix de départ) ; le garde-fou reste pleinement actif dès la 2e navigation. Revérifié en conditions réelles (smoke puis campagne complète) : T8 3/3, T9 3/3 (contre 0/3 et blocages similaires avant)

### 41. `tests_integration/test_web_tasks.py`/`scripts/run-campaign.sh` (mode smoke) — `WEB_TASKS_SMOKE_TASKS=T1,...` matchait aussi `T10_*`/`T11_*` (`startswith` sur un…

**Symptôme / cause confirmée** : `WEB_TASKS_SMOKE_TASKS=T1,...` matchait aussi `T10_*`/`T11_*` (`startswith` sur un préfixe numérique partagé) — un smoke ciblé sur T1 exécutait en fait aussi T10 et T11 sans le vouloir, faussant l'estimation de durée et le rapport

**Correctif** : Frontière `_` exigée (`t[0] == p or t[0].startswith(p + "_")`), corrigée aux deux endroits (le filtre réel dans `test_web_tasks.py` ET l'estimation dans `run-campaign.sh`, qui dupliquait la même logique)

### 42. `tests_integration/test_web_tasks.py` (isolation GhostDesk, PAS un bug du cœur cognitif) — Investigation T9 (voir docs/history.md) : un thread bloqué par le garde-fou anti-fabrication…

**Symptôme / cause confirmée** : Investigation T9 (voir docs/history.md) : un thread bloqué par le garde-fou anti-fabrication sur `browser_navigate` a pris un `screen_shot` (GhostDesk) et lu un Firefox déjà ouvert sur insee.fr depuis plus de 10h — résidu d'un `app_launch` lancé par un thread T9 complètement différent, des heures plus tôt. Contrairement à la session Playwright (déjà isolée, voir plus haut), GhostDesk pilote un vrai bureau à l'échelle de la MACHINE, sans aucun rapport avec le thread langgraph-agent en cours ni avec la tâche — un « succès » qui ne prouvait rien sur la capacité de l'agent à refaire la tâche à froid

**Correctif** : `_reset_ghostdesk_desktop()` (`pkill -f firefox` sur le conteneur `ghostdesk`) appelé avant CHAQUE répétition, même garantie que `_reset_browser_session`/`_purge_downloads_volume` — revérifié : le Firefox résiduel est bien tué, plus de contamination sur le smoke T9 suivant

### Fausse alerte écartée : monkeypatch global de httpx.AsyncClient

Un test utilisait un monkeypatch global de `httpx.AsyncClient` pour simuler les appels HTTP vers les autres microservices, ce qui cassait par effet de bord le client interne du SDK `openai` (qui construit ses propres classes comme sous-classes de `httpx.AsyncClient`). La suite de tests finale utilise `respx`, qui patche au niveau du transport HTTP sans jamais toucher à la hiérarchie de classes.

### 43. `services/mcp-client/app/main.py` (normalisation `target`, tous outils `browser_*` à ref) — Diagnostic B-β hard (benchmark v2, 2026-07-31) : hypothèse BULK_CHECK_DIRECTIVE rejetée par archives, creusé la vraie cause de la bascule vers `browser_evaluate`…

**Symptôme / cause confirmée** : Diagnostic B-β hard (benchmark v2, 2026-07-31) : hypothèse BULK_CHECK_DIRECTIVE rejetée par archives (le raisonnement du modèle ne la mentionne jamais aux points de bascule), creusé la vraie cause de la bascule vers `browser_evaluate`. Le modèle recopie parfois l'annotation `[ref=e7]` de `browser_snapshot` telle quelle comme valeur de `target` — Playwright n'accepte que le jeton nu (`e7`) ou un sélecteur CSS ; `"ref=e7"` est interprété comme un moteur de sélecteur inconnu (`Unknown engine "ref"`) et échoue systématiquement. Mesuré sur l'historique complet des audit logs, toutes fixtures confondues : **28/28 échecs** avec le préfixe `ref=`, **33/35 succès** sans (les 2 échecs restants ont une cause distincte, schéma). Défaut présent depuis `fixture-hr-app` le **2026-07-22**, donc antérieur et transverse à toutes les campagnes depuis — pas circonscrit à `fixture-admin`/famille B. Sur les runs B-β hard, le repli DEVIENT une brèche de policy uniquement quand il tombe sur `browser_evaluate` (NEVER_GRANTABLE) plutôt que sur un sélecteur CSS deviné avec succès — question de chance sur l'essai, pas un mécanisme différent.

**Correctif** : `_normalize_ref_targets` (`services/mcp-client/app/main.py`) réécrit tout `"ref=eN"`/`"ref=fMeN"` en jeton nu AVANT dispatch, appliqué génériquement à toute clé `target`/`startTarget`/`endTarget` (y compris imbriquée, ex. `browser_fill_form.fields[]`) — couvre tous les outils actuels et futurs sans liste à maintenir. Filet : `_rewrite_ref_error` reformule tout message `Unknown engine "..." while parsing selector` résiduel en redirection exploitable (jeton nu ou sélecteur CSS) plutôt que de renvoyer l'erreur brute de Playwright. `browser_inspect` (nouvel outil TIER_READ, template JS fixe, `_build_inspect_call`) ajouté en complément : le repli d'introspection DOM légitime n'a plus besoin de `browser_evaluate`. Revérifié par la suite de tests (40 tests `mcp-client`, 435 tests `langgraph-agent`, tous verts).

**Note de lecture pour toute campagne antérieure** : les mesures de latence et de tool_calls/tâche de TOUTES les campagnes avant ce correctif (cœur cognitif inclus) incluaient le coût des 2 à 4 essais de sélecteur perdus à chaque formulaire rencontré — un biais CONSTANT, présent identiquement dans le point zéro et les mesures suivantes, donc ne remettant en cause aucune comparaison déjà faite. Mais la baisse de tool_calls/tâche attendue sur B-α, T2 et A4 après ce correctif ne doit PAS être relue comme un gain du cœur cognitif ou d'un autre mécanisme mesuré séparément : c'est la disparition d'un coût qui était déjà là, pas un effet nouveau.

### 44. `langgraph-agent` (`/approve`, `app/main.py`) — Trouvé en construisant l'exercice compaction multi-tours (`tests_integration/probe_compaction_multi_turn.py`, premier client à enchaîner plusieurs tours de haut niveau sur le même thread)…

**Symptôme / cause confirmée** : Trouvé en construisant l'exercice compaction multi-tours (`tests_integration/probe_compaction_multi_turn.py`, premier client à enchaîner plusieurs tours de haut niveau sur le même thread avec approbation à chaque tour — `session_grants` est remis à `[]` par `_resolve_run` sur CHAQUE nouveau message utilisateur, donc chaque tour redemande son approbation). Sur le smoke live du 2026-07-31 (fil `code_interne`), un tour cense être un simple aller-retour (prix KX-4471) a produit une réponse incohérente évoquant la connexion HR et le rappel d'un code — contenu appartenant à un TOUT AUTRE tour, déjà répondu. Cause : `/approve` calculait `owui_message_count = len(request.messages)` en supposant TOUJOURS la convention du bouton Open WebUI (qui édite en place le message "⚠️ Approbation requise" — son `messages` inclut donc déjà un emplacement pour ce tour). Un client qui AJOUTE la réponse finale comme un nouveau message au lieu d'éditer en place (le harnais multi-tours, comme `_approve()` de `test_web_tasks.py` avant lui, jamais testé en enchaînement) envoie un `messages` plus court d'un cran — sans détection, le tour suivant réinjectait le contenu déjà répondu du tour précédent comme s'il était nouveau (`request.messages[already_seen:]`), le déficit s'accumulant à chaque tour supplémentaire nécessitant une approbation. **Défaut latent et documenté dans le docstring lui-même depuis l'origine du endpoint, jamais déclenché avant faute de client multi-tours existant.**

**Correctif** : `/approve` compare désormais `len(request.messages)` reçu au compte déjà persisté pour ce thread (`owui_message_count` au moment de la pause, avant mise à jour) pour détecter la convention du client — égal au compte moins un : le client ajoutera une nouvelle réponse, anticiper cette croissance (`+1`) ; sinon (placeholder déjà inclus, ou compte inattendu) : conserver tel quel, comme avant. Les deux conventions fonctionnent sans connaissance mutuelle ni connaissance de la logique interne de ce endpoint. Docstring réécrit en conséquence (ce n'est plus une contrainte imposée aux clients). Revérifié : 2 nouveaux tests (`test_approve_append_convention_then_new_turn_message_count`, `test_approve_append_convention_across_consecutive_approval_turns`, `tests/test_multi_turn_persistence.py`) échouent tous deux SANS le correctif (vérifié par reversion manuelle) et passent avec — le test existant de la convention Open WebUI (`test_tool_approval_then_new_turn_message_count`) reste vert, aucune régression. Suite complète 437/437.

### 45. `services/langgraph-agent/app/approval_policy.py` (tiers `browser_snapshot`/`browser_take_screenshot`) — Trouvé en construisant la sonde de faisabilité canal visuel (docs/architecture/visual-channel-feasibility.md)…

**Symptôme / cause confirmée** : Trouvé en construisant la sonde de faisabilité canal visuel (`docs/architecture/visual-channel-feasibility.md`) : `browser_snapshot` et `browser_take_screenshot` restaient TIER_SENSITIVE par défaut — une pause d'approbation humaine pour REGARDER une page, sans effet de bord ni rien à exfiltrer — alors que le serveur MCP Playwright officiel les déclare lui-même `type: "readOnly"` (vérifié contre le schéma de l'image `mcp/playwright:latest` installée, CLAUDE.md #8). Même raisonnement déjà appliqué à `browser_extract`/`browser_inspect`, jamais étendu à ces deux-là.

**Correctif** : ajoutés à `_DEFAULT_TIER_READ` (`app/approval_policy.py`). Les tiers d'approbation étant un comportement mesuré (CLAUDE.md), un smoke restreint (1 tâche, T1, `scripts/run-campaign.sh --tasks T1 --reps 1`) a été joué avant toute campagne de comparaison : **succès (1/1)**, et l'audit log brut confirme le changement de comportement — les appels `browser_snapshot` du run (plus d'une dizaine, catalogue paginé) n'apparaissent PLUS du tout dans le journal (`TIER_READ` = « auto, silencieux », comme `browser_extract`), alors que `browser_navigate`/`browser_click` restent journalisés avec `tier: reversible`. Aucune pause d'approbation n'a été déclenchée pour `browser_snapshot` sur ce run. Un test existant (`tests/test_campaign_preflight.py::test_check_tools_schema_flags_desync_between_agent_and_mcp_client`) utilisait `browser_snapshot` comme exemple d'outil absent d'`EXPECTED_TOOLS` (union des tiers d'`approval_policy.py`) — devenu caduc puisqu'il y entre désormais ; remplacé par `browser_hover` (autre outil réel, toujours absent). 2 nouveaux tests ajoutés (`test_approval_policy.py`), suite complète 438/438. Campagne de comparaison friction/tool_calls à part entière non lancée (hors périmètre de ce correctif, smoke restreint uniquement) — à faire lors d'une prochaine mesure officielle si un point de comparaison est nécessaire.

### 46. `services/langgraph-agent/app/main.py` (`_resolve_run`, `session_grants` remis à zéro par tour) — Trouvé en réglant une question consignée sans être traitée lors du chantier benchmark v2…

**Symptôme / cause confirmée** : Trouvé en réglant une question consignée sans être traitée lors du chantier benchmark v2 (voir docs/history.md, "A4 / COMPACTION"). `session_grants` (`AgentState`, `app/graph.py`) porte son propre commentaire — « capped at TIER_REVERSIBLE... for the rest of the thread » — et le README publie la même promesse (« reversible writes are covered by a session grant »). Pourtant `_resolve_run` remettait `session_grants` à `[]` dans `run_input` sur CHAQUE nouveau tour utilisateur de haut niveau, exactement comme `tool_iterations`/`plan`/`replan_count` — des champs qui, eux, DOIVENT se réinitialiser par tour, contrairement à un grant de session. Un grant obtenu au tour 1 (« approuver pour la session ») exigeait donc une nouvelle approbation au tour 2 pour le MÊME outil — contredisant les deux textes qui documentent son comportement voulu. Jamais détecté avant : aucun client multi-tours n'existait pour l'exercer (même raison que le défaut `/approve`, #44).

**Correctif** : la clé `"session_grants"` retirée du dictionnaire `run_input` (`_resolve_run`) — une mise à jour d'état PARTIELLE qui omet une clé laisse la valeur déjà persistée intacte (le motif `state.get("session_grants") or []`, déjà utilisé partout où ce champ est lu, gère nativement le cas d'un tout premier tour sans valeur persistée). `plan_grant`/`plan_grant_session` restent réinitialisés à chaque tour, sans changement — leur propre commentaire les scope explicitement « within the same task », une nouvelle tâche méritant un nouvel accord de plan.

**Tests** : nouveau test (`test_session_grant_persists_across_a_new_top_level_turn`, `tests/test_multi_turn_persistence.py`) — échoue sans le correctif (vérifié par reversion manuelle), passe avec. Suite complète 439/439.

**Smoke restreint (2 tours, live)** : un premier essai a d'abord semblé infirmer le correctif (`browser_navigate` redemandait une approbation au tour 2) — investigation : le script de smoke avait accordé « pour la session » à la MAUVAISE pause (une pause de PLAN, mécanisme distinct et à raison réinitialisé par tour, pas la pause d'outil `browser_navigate`), un bug du script de test, pas du correctif. Reprise avec un script corrigé (accorde spécifiquement la première pause D'OUTIL) : tour 2 ne redemande plus jamais d'approbation pour `browser_navigate`, malgré son usage pour trouver un second produit (PX-1001) — une seule pause de plan (attendue, tâche différente), zéro pause d'outil. Confirme le correctif.

### 47. `docker-compose.yml` (`langgraph-agent` service) — found by effort 2 point 3's live smoke, first real run of the new `PLANNING_MODE` env var

**Symptom / confirmed cause**: the point-3 live smoke (`PLANNING_MODE=merged`, merged-planning mode) failed preflight: `PreflightError: PLANNING_MODE : attendu='merged' effectif=''`. `PLANNING_MODE` was read in `app/graph.py` (`os.environ.get("PLANNING_MODE", "nodes")`) and asserted by `campaign_preflight.py`'s `EXPECTED_AGENT_FLAGS`, but never declared in `docker-compose.yml`'s `langgraph-agent` service `environment:` block — unlike every other cognitive-core flag (`PLANNER_ENABLED`, `VERIFICATION_ENABLED`, `PLAN_VALIDATION_ENABLED`, `PLAN_JUDGE_ENABLED`), which all have an explicit `- VAR=${VAR:-default}` line there. The host shell export had no path into the container, so the effective value stayed `""` regardless of `--force-recreate` — a variant of the "operational trap" already named in CLAUDE.md (env vars read at import require a real value inside the container, not just the harness shell).

**Fix**: added `- PLANNING_MODE=${PLANNING_MODE:-nodes}` to `docker-compose.yml`, same declaration pattern as the other 4 flags. No code change needed elsewhere — `app/graph.py`'s default (`"nodes"`) and `campaign_preflight.py`'s expected value already matched; only the compose wiring was missing. Re-verified: preflight passed `PLANNING_MODE` on every one of the 6 live smokes that followed (see docs/history.md, "EFFORT 2", "Live smoke run by the user" — a separate, real finding surfaced by those runs, unrelated to this fix).
