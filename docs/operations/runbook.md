# Runbook — commandes de rebuild/redémarrage

Consolidé depuis les mentions existantes dans README.md/docs/architecture
(chantier restructuration, voir docs/briefs/restructuration-et-anglais.md,
phase 3) — pas de réécriture du contenu, juste un point d'entrée unique
pour ces commandes plutôt que dispersées.

- **`docker compose up -d`** : démarrage normal de la stack (voir README,
  Démarrage rapide).
- **`docker compose restart langgraph-agent`** : après un changement de
  schéma d'outils côté mcp-client (`_tools_schema_cache` est rempli une
  fois pour la durée du process et jamais invalidé — voir
  `campaign_preflight.check_tools_schema`).
- **`docker compose up -d --force-recreate langgraph-agent`** : après un
  changement de `.env` touchant une variable lue au niveau module de
  `app/graph.py` (ex. les flags du cœur cognitif, voir
  docs/architecture/autonomy.md) — un simple `restart` ne relit pas
  `.env`, le process ne redémarre pas.
- **`docker compose up -d --build tabbyapi`** : après un rebuild d'image
  TabbyAPI, pour que le conteneur qui tourne corresponde à la dernière
  image construite (`campaign_preflight.check_tabbyapi_image_fresh`
  refuse une campagne sinon).
- **`docker compose down` / `up`** : préserve les volumes nommés
  (`qdrant-data`, `open-webui-data`) — **`docker compose down -v`** les
  supprime, à ne jamais taper sans le vouloir explicitement (voir
  docs/architecture/observability.md, Persistance des données).
