# Observabilité et persistance des données

Contenu déplacé tel quel depuis README.md (chantier restructuration, voir docs/briefs/restructuration-et-anglais.md, phase 3) — pas de réécriture à ce stade.

Cockpit web local en une page (http://localhost:8090 par défaut,
`DASHBOARD_PORT`) : métriques d'inférence llama-server (débit decode/prefill
en tok/s, contexte occupé par slot), composition détaillée du contexte
construit par langgraph-agent (system prompt, skills, schéma d'outils,
historique, images — voir `POST /context` plus bas) et VRAM des GPU.

**Architecture** : `GET /api/snapshot` agrège en parallèle, chaque source en
best-effort (une source en panne renvoie sa section à `null`, jamais une 500
globale, statut 200 systématique — le dashboard poll ce endpoint toutes les
2s) : `llama-server` (`/metrics`, format Prometheus parsé par un parser
minimal maison, `app/prometheus.py` ; `/slots`), `langgraph-agent`
(`/threads/recent` puis `POST /context` pour le thread résolu) et
`nvidia-smi` en subprocess (VRAM, `app/gpu.py`). La page (`GET /`, HTML/JS
vanille, aucune dépendance externe) ne parle jamais directement à
llama-server/langgraph-agent : seuls Open WebUI et le dashboard ont un port
publié sur l'hôte, tout le reste n'est joignable que via le réseau interne
`agent-net` — d'où l'agrégation côté backend du dashboard plutôt que des
appels depuis le navigateur.

**`POST /context` (langgraph-agent, `app/graph.py:describe_context`)** :
décompose le contexte persisté d'un thread en blocs approximatifs
(`system`/`skills`/`tools_schema`/`history_text`/`images`/`pending`), chacun
avec un compte de tokens estimé (`estimate_tokens`, ~3.5 caractères/token —
pas un tokenizer exact, volontairement hors périmètre, voir plus bas) et un
forfait fixe par image (`IMAGE_TOKEN_ESTIMATE`, défaut `1500`, un compte
exact dépendrait du tokenizer visuel du modèle servi). Le schéma d'outils est
mesuré depuis le cache déjà rempli par `_get_bound_llm` (jamais recalculé :
`/context` reste strictement lecture seule, comme `/pending`). Thread inconnu
du checkpointer -> 200 avec des blocs vides plutôt qu'une 404, pour ne pas
transformer le polling continu du dashboard en bruit d'erreurs côté client.

**`GET /tools/schema` (langgraph-agent)** : noms d'outils tels
qu'EFFECTIVEMENT vus par ce process (`_tools_schema_cache`), pas ceux servis
par mcp-client au moment de l'appel — la distinction a mordu en conditions
réelles (voir docs/history.md, "bug de cache de schéma d'outils") : ce cache est
rempli une fois pour la durée du process et jamais invalidé, donc un
redémarrage de mcp-client seul peut laisser langgraph-agent répondre un
schéma périmé. Lecture seule, comme `/pending`/`/context`. Consommé par le
préambule de campagne du harnais de tâches web
(`tests_integration/campaign_preflight.py`, voir
`docs/briefs/phase-1-coeur-cognitif.md`) pour refuser une campagne AVANT son
premier run si ce schéma est désynchronisé de celui de mcp-client.

**Sélection de thread (`GET /threads/recent`)** : langgraph-agent n'a jamais
d'identifiant de conversation stable côté Open WebUI (voir plus bas,
`_derive_thread_id`) ; un registre en mémoire process, jamais persisté
(cohérent avec le checkpointer `MemorySaver` lui-même en mémoire), retient
les 5 threads vus le plus récemment (alimenté par `/v1/chat/completions` et
`/approve`, jamais par les endpoints purement lecture seule `/pending` ou
`/context` eux-mêmes). La page sélectionne le plus récent par défaut, avec un
menu déroulant pour en choisir un autre.

**VRAM (`ENABLE_GPU_STATS`, défaut `false`)** : `nvidia-smi --query-gpu=...
--format=csv,noheader,nounits` en subprocess, désactivé par défaut — nécessite
le runtime nvidia (bloc `deploy` commenté dans `docker-compose.yml`, à
décommenter avec cette variable) pour que le binaire `nvidia-smi` soit
présent dans le conteneur `python:3.12-slim` du dashboard, qui n'a sinon
aucun besoin d'accès GPU.

Hors périmètre explicite (voir demande initiale) : Prometheus/Grafana,
Langfuse, persistance des métriques (tout est en mémoire, perdu au
redémarrage), alerting, auth (réseau local), WebSocket/SSE (le polling 2s
suffit), télémétrie de tâches (taux de succès), tokenizer exact.


## Persistance des données

Deux volumes Docker nommés persistent à travers les redémarrages et les
`docker compose down` / `up` (mais pas `docker compose down -v`, qui les
supprime) :

- **`qdrant-data`** : contenu des collections `documents` et `memory` de
  `context-manager` (RAG et mémoire long-terme).
- **`open-webui-data`** (`/app/backend/data`) : conversations, comptes
  utilisateurs, fichiers uploadés et paramètres d'Open WebUI (base SQLite
  interne à l'image).

Trois répertoires montés en bind mount persistent nativement, puisqu'ils
vivent directement sur le système de fichiers de l'hôte, indépendamment du
cycle de vie des conteneurs : `./workspace`, `./skills`, `./models`.

**Point de vigilance corrigé** : `WEBUI_SECRET_KEY` n'était fixé nulle part.
Sans cette clé fixe, Open WebUI en génère une nouvelle à chaque recréation de
conteneur, ce qui invalide toutes les sessions de connexion (et empêche de
déchiffrer d'éventuels secrets stockés, comme des jetons OAuth) même si les
données elles-mêmes restent intactes dans le volume. Corrigé : la clé se
configure maintenant via `.env` (voir `.env.example`), à générer une seule
fois avec `openssl rand -hex 32`.

Les autres services (`skill-manager`, `mcp-client`, `mcp-terminal`) sont sans
état. `langgraph-agent` reste conceptuellement sans état lui non plus : c'est
Open WebUI qui renvoie l'historique complet de la conversation à chaque
requête `/v1/chat/completions`, pas `langgraph-agent` qui le conserve de façon
persistante. Il compile toutefois désormais son graphe avec un checkpointer
(`MemorySaver`, **en mémoire seulement**), nécessaire pour la supervision
humaine des appels d'outils (voir section suivante) : un redémarrage du
service perd toute approbation en attente, ce qui relance simplement une
conversation "fraîche" pour le thread concerné — aucune donnée n'est donc
réellement perdue au sens propre.

