# agentic-ai-playground

![Logo](docs/assets/logo.png)

Stack Docker Compose pour un agent IA local : Open WebUI → LangGraph Agent →
(Skill Manager / Context Manager / MCP Client) → TabbyAPI.

## Démarrage rapide

```bash
cp .env.example .env
# éditer .env : WORKSPACE_HOST_PATH doit être le chemin ABSOLU de ./workspace sur l'hôte
# (requis car mcp-client monte ce chemin dans des conteneurs qu'il spawn lui-même)
# placer le quant EXL3 du modèle (safetensors + config.json + tokenizer,
# format HuggingFace) sous ./models/agent-llm/ — backend TabbyAPI par défaut,
# jamais téléchargé automatiquement (voir docs/architecture/inference-backend.md).
# Pour le backend alternatif llama-server (.gguf), éditer .env :
# LLAMA_MODEL_FILE/LLAMA_MMPROJ_FILE doivent correspondre aux fichiers
# réellement présents dans ./models

docker pull mcp/filesystem:latest
docker pull mcp/git:latest
docker pull mcp/playwright:latest   # serveur HTTP persistant (service playwright-mcp), voir docs/resolved-bugs.md
docker compose --profile build-only build mcp-terminal-build   # construit l'image locale mcp-terminal:local

docker compose up -d
```

Interface accessible sur http://localhost:3000 (Open WebUI). Commandes de
rebuild/redémarrage : voir `docs/operations/runbook.md`.

## Arborescence

```
docker-compose.yml
.env.example
requirements-test.txt   dépendances de test communes (pytest, respx)
services/
  langgraph-agent/   API compatible OpenAI + graphe LangGraph (autonomie,
                     supervision humaine — voir docs/architecture/)
    app/
    tests/
  skill-manager/      liste/sélectionne les skills (./skills)
    app/
    tests/
  context-manager/    RAG + mémoire (Qdrant + sentence-transformers)
    app/
    tests/
  mcp-client/          spawn filesystem/git/terminal à la demande (docker.sock) ;
                       browser/desktop/ocr sont des serveurs HTTP persistants
                       (mcp-client s'y connecte en Streamable HTTP)
    app/
    tests/
  mcp-terminal/        serveur MCP "terminal" maison, liste blanche stricte
    server.py
    tests/
  ghostdesk/           image officielle YV17labs, bureau virtuel piloté par
                       l'agent (service docker-compose à part, Streamable HTTP)
  playwright-mcp/      image officielle mcp/playwright, navigateur piloté par
                       l'agent (service docker-compose à part, serveur HTTP
                       natif — voir docs/resolved-bugs.md)
  llama-server/        build du fork llama.cpp servant le modèle (backend
                       alternatif — voir docs/architecture/inference-backend.md)
  ocr-service/         OCR d'appoint pour le grounding du VLM (PaddleOCR CPU,
                       find_text/read_screen — voir docs/architecture/autonomy.md)
    app/
    tests/
  dashboard/           Cockpit d'observabilité local — voir
                       docs/architecture/observability.md
    app/
      static/          page HTML/JS vanille servie telle quelle (pas de build)
    tests/
skills/     à remplir (un sous-dossier par skill, avec un SKILL.md)
workspace/  partagé avec les serveurs MCP filesystem/git/terminal, ainsi
            qu'avec langgraph-agent pour le journal d'audit (.audit/, voir
            docs/architecture/tool-supervision.md)
models/     poids (.gguf) du modèle et du projecteur multimodal servis par
            llama-server — jamais téléchargés automatiquement, voir
            docs/architecture/inference-backend.md
```

## Documentation

- `docs/architecture/inference-backend.md` — TabbyAPI/llama-server, conversion
  d'images, thinking adaptatif.
- `docs/architecture/autonomy.md` — boucle plan → agir → vérifier →
  replanifier (Phase 1 « cœur cognitif »), OCR d'appoint.
- `docs/architecture/tool-supervision.md` — approbation humaine, tiers de
  réversibilité, grants de session, journal d'audit.
- `docs/architecture/observability.md` — dashboard, persistance des données.
- `docs/operations/testing.md` — suites de tests par service, streaming SSE.
- `docs/operations/runbook.md` — commandes de rebuild/redémarrage.
- `docs/project-status.md` — état d'avancement (change à chaque checkpoint).
- `PLAN.md` — feuille de route (change rarement, source de vérité).
- `docs/history.md` / `docs/resolved-bugs.md` — journal d'avancement et bugs résolus
  (se consultent par recherche ciblée, jamais en entier — voir `CLAUDE.md`).
- `docs/briefs/` — briefs de chantier en cours.

## Limites connues assumées (choix de conception, pas des bugs)

- **`mcp-terminal` n'expose pas de shell libre** : liste blanche stricte
  (`ls`, `pwd`, `cat`, `git status`), confinée à `/workspace`. Étendre cette
  liste avec prudence : chaque commande ajoutée est une nouvelle surface
  d'attaque potentielle.
- **`mcp-client` monte `/var/run/docker.sock`** : équivaut à un accès root sur
  l'hôte. Acceptable en usage local ; à remplacer par un socket-proxy filtrant
  avant toute exposition réseau.
- **Matching de skills et RAG volontairement simplistes** (mot-clé naïf, pas
  de reranker) — à muscler si le volume de skills/documents grossit.
- **`ghostdesk` (serveur MCP "desktop") tourne avec `cap_add: SYS_ADMIN` et
  expose un shell** : surface d'attaque bien plus large que `mcp-terminal`
  (pas de whitelist, contrôle GUI complet). À ne jamais exposer au-delà du
  réseau interne `agent-net` — seul le port noVNC (6080) est publié sur
  l'hôte, volontairement, pour observer l'agent piloter le bureau ; le port
  MCP (3000) ne l'est pas. `mcp-terminal` reste l'outil par défaut pour les
  commandes simples ; `ghostdesk` n'est sollicité que pour du pilotage GUI
  qui le justifie réellement — les deux coexistent sciemment plutôt que de
  remplacer l'un par l'autre. Accès : http://localhost:6080 une fois le
  service démarré, mot de passe = `GHOSTDESK_VNC_PASSWORD` (voir `.env`).
- **Limite historique levée** : les outils de capture d'écran/clic guidé de
  `ghostdesk` n'étaient pas exploitables par l'agent tant que le modèle
  servi (Qwen2.5-Coder, via vLLM) n'était pas multimodal. Le backend par
  défaut est désormais `llama-server` (voir docs/architecture/inference-backend.md),
  servant Qwen3.6-35B-A3B avec un projecteur multimodal (`--mmproj`) —
  l'agent peut donc désormais recevoir et interpréter les captures d'écran
  GhostDesk. Reste néanmoins une limite distincte, désormais atténuée mais
  pas résolue : la précision du grounding (viser le bon élément à l'écran)
  d'un modèle de vision généraliste. `ocr-service` (voir
  docs/architecture/autonomy.md) compense pour les éléments TEXTUELS via
  `find_text`/`read_screen` (coordonnées OCR exactes plutôt qu'une
  estimation visuelle) ; les éléments sans texte (icônes) restent estimés
  visuellement par le VLM, sans détection d'éléments UI dédiée (type
  OmniParser, explicitement hors périmètre pour l'instant).
- **`ghostdesk` est un serveur MCP HTTP persistant avec état** (bureau/session
  VNC), contrairement aux autres serveurs MCP du projet qui sont spawnés en
  STDIO éphémère par `mcp-client` (`docker run -i --rm` par appel). Il tourne
  en continu comme service `docker-compose` à part ; `mcp-client` s'y
  connecte via `streamablehttp_client` (SDK `mcp` ≥ 1.8, d'où le bump de
  `mcp==1.2.0` vers `mcp==1.9.4` dans `services/mcp-client/requirements.txt`),
  authentifié par bearer token (`GHOSTDESK_AUTH_TOKEN`, voir `.env.example`).
- **`playwright-mcp` (serveur "browser") est un serveur HTTP persistant
  depuis le correctif documenté en détail dans `docs/resolved-bugs.md`** — auparavant
  spawné en STDIO éphémère (`docker run -i --rm mcp/playwright:latest` par
  appel), il perdait tout état de navigation entre deux appels d'outils.
  L'image officielle expose nativement un mode serveur HTTP
  (`--host 0.0.0.0 --port 8931`, endpoint Streamable HTTP `/mcp`) ; ceci ne
  suffisait cependant PAS à lui seul, car Playwright MCP scope son contexte
  navigateur (page, cookies, historique) à la SESSION MCP et non au process
  serveur — `mcp-client` doit donc en plus garder la session "browser"
  ouverte entre deux appels HTTP (`_get_persistent_session`/
  `_persistent_sessions` dans `services/mcp-client/app/main.py`), au lieu
  d'en rouvrir une neuve à chaque fois comme pour les autres serveurs.
- **Volume de téléchargement partagé `agent-downloads`** (Phase 1d-révisée,
  voir docs/history.md, T5) : `playwright-mcp` garde son profil navigateur
  `--isolated` (en mémoire, jamais persisté), mais un téléchargement
  déclenché dans la page (lien/bouton avec `Content-Disposition:
  attachment`) atterrit désormais dans un chemin EXPLICITE et partagé
  (`--output-dir=/downloads`, volume nommé `agent-downloads`) plutôt que
  dans le filesystem interne du conteneur (défaut réel constaté :
  `/home/node/.playwright-mcp/`, jamais deviné correctement par le modèle).
  Le serveur MCP filesystem monte ce même volume en LECTURE SEULE
  (`services/mcp-client/app/main.py`, racine `/downloads` en plus de
  `/projects`) : on partage l'artefact téléchargé, jamais l'état du
  navigateur. Le system prompt documente ce chemin explicitement
  (`DOWNLOAD_DIRECTIVE`, `app/graph.py`) plutôt que de laisser le modèle en
  deviner un.
- **Précision des clics avec les modèles Qwen** : ces modèles raisonnent
  nativement en repère de coordonnées normalisé 0-1000, alors que GhostDesk
  attend par défaut des pixels écran natifs (documenté par GhostDesk) — sans
  correction, les clics atterrissent à côté de leur cible. `mcp-client`
  envoie donc l'en-tête `GhostDesk-Model-Space` (valeur `GHOSTDESK_MODEL_SPACE`,
  défaut `1000`) sur chaque appel HTTP vers GhostDesk (`_run_on_server`,
  `services/mcp-client/app/main.py`). À vider (`GHOSTDESK_MODEL_SPACE=`) si
  le modèle servi passe à un modèle frontière (Claude, GPT-4o), qui travaille
  nativement en pixels écran. Ce fix ne résout pas le grounding en soi (viser
  le bon élément reste imprécis avec un modèle de vision généraliste) — voir
  la limite ci-dessus sur l'absence d'OCR/détection d'éléments UI.
- **Mémoire long-terme (`context-manager`) jamais branchée à la conversation** :
  `POST /remember` (stocke un fait lié à un `user_id`, collection Qdrant
  `memory`) et `POST /retrieve` avec `collection="memory"` existent et sont
  testés au niveau de `context-manager` lui-même, mais rien dans
  `langgraph-agent` ne les appelle. Le nœud `retrieve_context`
  (`app/graph.py`), qui tourne automatiquement à chaque tour, n'interroge
  QUE la collection `documents` (RAG) — jamais `memory`. Concrètement : un
  souvenir stocké via `/remember` ne remonte jamais tout seul dans une
  conversation, et il n'existe aujourd'hui aucun outil MCP ni commande slash
  pour en stocker ou en rappeler un depuis le chat — seul un appel direct à
  l'API `context-manager` (curl, etc.) permet de s'en servir.
