# Autonomie — boucle plan → agir → vérifier → replanifier

Contenu déplacé tel quel depuis README.md (chantier restructuration, voir docs/briefs/restructuration-et-anglais.md, phase 3), OCR d'appoint inclus (outil de grounding servant la même boucle) — pas de réécriture à ce stade.

**Architecture de la boucle** (voir `docs/briefs/phase-1-coeur-cognitif.md`
pour le chantier complet, séquencé en 4 itérations, une itération = un
mécanisme = un juge désigné = un checkpoint) : `plan_task` décompose
l'objectif en sous-tâches JSON validées, `validate_plan` les fait passer
par un pipeline heuristiques puis (optionnel) juge LLM avant approbation
humaine tierée, `call_llm`/`_execute_tool_calls` exécutent, `verify_action`
compare chaque résultat au critère de la sous-tâche active, `replan_task`
reprend la main sur échec de budget, `report_failure` termine honnêtement
si le budget de replanification est épuisé. Les 4 mécanismes sont
indépendamment activables (`PLANNER_ENABLED`/`VERIFICATION_ENABLED`/
`PLAN_VALIDATION_ENABLED`/`PLAN_JUDGE_ENABLED`) — **défauts INVERSÉS à
`true`** depuis la campagne finale (29/33, cohérente avec la Campagne A
pré-cœur-cognitif à 30/33 — voir docs/briefs/flags-du-coeur-cognitif.md et
docs/history.md) : le cœur cognitif est mesuré et adopté, c'est désormais la
DÉSACTIVATION qui doit être explicite. Voir le détail de chacun ci-dessous.

**⚠️ Piège d'exploitation** : ces 4 flags (ainsi que
`MAX_TOOL_ITERATIONS`/les budgets de tentatives et replanification/
`PLANNER_THINKING_ENABLED`/les overrides de tiers/les seuils de
tronquage — liste complète dans `EXPECTED_AGENT_FLAGS`,
`tests_integration/campaign_preflight.py`) sont lus au niveau MODULE de
`app/graph.py` (constantes Python calculées une seule fois à l'import) :
tout changement de `.env` exige
`docker compose up -d --force-recreate langgraph-agent` — un simple
`restart` ne relit PAS `.env`, le process ne redémarre pas. Les définir
dans le shell qui lance `scripts/run-campaign.sh` **n'a AUCUN effet** : le
harnais parle à l'agent en HTTP (`docker exec ... curl`), ces flags vivent
dans le process serveur du conteneur, pas dans l'environnement du harnais
— seule la modification de `.env` PUIS le `--force-recreate` ci-dessus
change le comportement mesuré. `campaign_preflight.check_agent_flags()`
refuse désormais une campagne AVANT son premier run si les flags effectifs
du conteneur divergent de la config attendue, pour attraper ce piège tôt.

`plan_task` (`app/graph.py`,
nouveau nœud entre `select_skill` et `call_llm`) décompose l'objectif de la
tâche en sous-tâches JSON (`{description, critere_succes, outils}`, schéma
validé programmatiquement, 1 à 8 éléments) via un appel LLM dédié — non lié
aux outils (`planner_llm.ainvoke`, pas `bound_llm`), non streamé, séparé de
la boucle principale. `planner_llm` est un client `ChatOpenAI` SÉPARÉ de
`llm` (boucle conversationnelle), avec son propre budget `PLANNER_MAX_TOKENS`
(défaut `8192`, bien plus large que `LLM_MAX_TOKENS`) : bug réel trouvé en
conditions réelles (voir docs/history.md, Itération 3) — Qwen3.6/TabbyAPI
raisonne dans un champ `reasoning_content` séparé de `content` avant de
répondre, et ce raisonnement consommait à lui seul tout `LLM_MAX_TOKENS`
(2048), tronquant systématiquement la réponse JSON. Le message utilisateur
envoyé au planificateur inclut aussi la liste réelle des outils MCP
disponibles (`_available_tools_hint`, même raison : sans elle, le
planificateur invente des noms d'outils plausibles mais inexistants).
Calculé UNE SEULE fois par tâche (`AgentState.plan`, remis à `[]` à chaque
nouveau message utilisateur top-level comme `observed_urls`) : toute erreur
(transport, JSON invalide) dégrade sur un plan à sous-tâche unique
enveloppant l'objectif tel quel, ne bloque jamais la tâche. Le plan est
visible dans les logs et résumé dans le message d'approbation existant
(`_format_plan_summary`, `app/main.py`).

**Pourquoi le défaut "false" d'origine** (Itération 1, avant la mesure
complète) : un second appel LLM en tête de chaque tâche aurait cassé la
quasi-totalité des tests existants, qui mockent une séquence fixe de
réponses sur `/v1/chat/completions` — voir docs/history.md, "Itération 1 : plan
explicite". Les tests concernés forcent désormais explicitement la valeur
qu'ils testent (fixture `_default_cognitive_core_flags_to_false`,
`tests/conftest.py`) plutôt que de dépendre du défaut.

**Vérification post-action + budget d'échec** (`VERIFICATION_ENABLED`,
défaut `true` — Itération 2) : **n'a d'effet que si `PLANNER_ENABLED` est
aussi activé** (rien à vérifier sans plan). Après chaque tour d'exécution
d'outils, `verify_action` (`app/graph.py`) compare le résultat au
`success_criterion` de la sous-tâche ACTIVE du plan, via un appel LLM juge
dédié (`{"atteint": bool, "raison": str}`, validé par
`_validate_verification_json`, même pipeline que le planificateur) — pas un
critère reformulé à la volée dans le raisonnement du tour (aucun
raisonnement structuré n'existe dans ce graphe pour l'extraire fiablement,
voir docs/history.md "Itération 2"). Verdict positif : sous-tâche `"fait"`,
avance à la suivante. Verdict négatif : `SUBTASK_ATTEMPT_BUDGET` tentatives
(défaut `3`) avant de marquer `"echoue"` — chaque retry doit changer de
stratégie, un tool_call identique (nom+args) au tour précédent après un
premier échec est bloqué par `_execute_tool_calls` sans appeler mcp-client
(`_repeated_strategy_feedback`). Sous-tâche `"echoue"` → replanification
(`replan_task`, réutilise le planificateur avec le contexte de l'échec,
`REPLAN_BUDGET` tentatives, défaut `2`) → au-delà, `report_failure` produit
un rapport honnête de l'état atteint (jamais un faux succès, jamais une
boucle infinie) et termine la tâche.

**Pipeline de validation du plan** (`PLAN_VALIDATION_ENABLED`, défaut
`true` — Itération 3) : **n'a d'effet que si `PLANNER_ENABLED` est aussi
activé**. `validate_plan` (`app/graph.py`, entre `plan_task`/`replan_task`
et `call_llm`) applique d'abord des heuristiques programmatiques
(`app/plan_validation.py` : bornes 2-12 sous-tâches, pas de doublons,
outils référencés existants, domaines dans le périmètre déclaré), puis, si
`PLAN_JUDGE_ENABLED` (défaut `true` depuis docs/briefs/
flags-du-coeur-cognitif.md — clause de retrait mesurée, voir docs/history.md
Itération 3 : a réellement vétoté un plan que les heuristiques laissaient
passer), un juge LLM
(`{"faisable": bool, "risques": [...], "etapes_manquantes": [...]}`,
FAIL-OPEN sur erreur). Rejet → `revise_plan` (max `PLAN_VALIDATION_CYCLES_MAX`
= 2 cycles) → au-delà, escalade humaine avec les motifs affichés. Plan
accepté : tier = pire tier parmi tous les outils déclarés (`_plan_tier`,
réutilise `approval_policy.tool_tier`) — `TIER_READ` passe direct,
`TIER_REVERSIBLE`/`TIER_SENSITIVE` déclenchent `require_plan_approval`
(miroir de `require_approval` mais pour le plan entier, nouveau champ
`plan_approved`). Grant de plan (`plan_grant`) possible pour
`TIER_REVERSIBLE` sur une replanification ultérieure de la même tâche,
**jamais pour `TIER_SENSITIVE`** (même philosophie que
`NEVER_GRANTABLE_TOOLS`). **Reste non fusionnable** avec l'approbation
individuelle d'un outil `TIER_SENSITIVE` à l'exécution — `require_approval`/
`_execute_tool_calls` inchangés, l'approbation du plan est un gate
additionnel en amont, jamais un substitut (vérifié en conditions réelles,
voir docs/history.md).

**Ancrage sur l'état réel de la page** (Itération 4, aucun nouveau flag —
fait partie de `VERIFICATION_ENABLED`/`PLAN_JUDGE_ENABLED`/
`PLAN_VALIDATION_ENABLED` existants) : trouvé en 2 temps sur sondes live
successives (voir docs/history.md, Itération 4, pour le détail des 6 sondes).
`verify_action` jugeait un `success_criterion` littéralement, sans jamais
voir la page réelle — un critère supposant une fonctionnalité absente (ex.
une barre de recherche) faisait échouer à tort une progression légitime
(ex. par pagination). `_fetch_verification_snapshot(objective)` capture un
`browser_snapshot` frais après tout tour utilisant un outil `browser_*`,
transmis au vérificateur (`etat_actuel_de_la_page`) — juge la progression
réelle, pas la lettre du critère. Le planificateur/juge de plan avaient le
même défaut d'ancrage à la replanification : `_grounding_snapshot(state,
objective)` (réutilise la fonction ci-dessus, `None` si aucune navigation
n'a encore eu lieu — le tout premier `plan_task` reste structurellement non
ancré) transmet le même snapshot à `revise_plan`/`replan_task`/
`_judge_plan`. Effet de bord découvert APRÈS ce second correctif : le
planificateur, désormais capable de voir de vrais noms de produits sur la
page, s'est mis à confondre l'élément exact demandé par l'objectif avec un
élément réel visible mais différent — les prompts (`snapshot_hint`,
`PLAN_JUDGE_SYSTEM_PROMPT`) mettent maintenant explicitement en garde
contre cette substitution.

**Campagnes v1 du chantier « cœur cognitif »** (11 tâches × 3 répétitions,
voir `docs/benchmark-v1.md` pour la suite v1 complète — sa
DERNIÈRE campagne de référence, la suite v1 approchant de la saturation) :

**Campagne finale** (4 flags actifs, ~104 min) : **29/33** après correctif
et repêchage (28/33 brut initialement — voir plus bas) — détail complet
dans `tests_integration/TASKS-BASELINE-post-coeur-cognitif.md`. Cohérent
avec la Campagne A pré-cœur-cognitif (30/33, voir docs/history.md), pas une
régression. Sur les 4 points manquants : 1 timeout infra du harnais (T7,
sans rapport avec l'agent), 1 échec d'extraction (T1), 2 échecs
d'extraction sur T8 (Wikipedia — voir ci-dessous). Score agrégé
volontairement affiché SANS le lisser : voir docs/history.md pour le détail
tâche par tâche.

| Tâche | Score | Note |
|---|---|---|
| T1 — extraction paginée | 2/3 | 1 échec extraction |
| T2 — formulaire congé | 3/3 | — |
| T3 — tableau dynamique | 3/3 | — |
| T4 — recherche multi-sauts | 3/3 | — |
| T5 — téléchargement + calcul | 3/3 | — |
| T6 — session authentifiée | 3/3 | — |
| T7 — impossible par construction | 2/3 | 1 timeout infra (harnais, pas l'agent) |
| T8 — Wikipedia | 1/3 (après repêchage) | 2 échecs extraction, 0 dépassement de contexte une fois les répétitions rendues indépendantes |
| T9 — Google/INSEE | 3/3 | — |
| T10 — books.toscrape | 3/3 | — |
| T11 — sonde de péremption | 3/3 | version consultée en direct à chaque fois |

**Bug de harnais trouvé et corrigé sur cette campagne** (`31aacac`, voir
docs/resolved-bugs.md) : les répétitions d'une même tâche dans `_run_campaign()`
partageaient leur `thread_id` (`_derive_thread_id` hache un prompt fixe,
identique entre répétitions) — T8 rep1 a fait déborder le contexte
(170285 tokens > 32768 côté TabbyAPI, une grosse page Wikipedia réelle +
plusieurs cycles de plan/vérification/juge), et les répétitions 2/3
rejouaient alors le MÊME thread déjà bloqué, ré-échouant à l'identique en
0.4s — lisant à tort T8 0/3 au lieu d'1 seul échec réel. Corrigé (marqueur
unique par répétition) et vérifié en direct (2 threads distincts, deux
exécutions pleinement indépendantes) avant de rejouer T8 seule pour le
score corrigé ci-dessus. Le dépassement de contexte réel sur des tâches
longues reste un effet de bord à traiter — confirme le besoin de la
Phase 2 (compaction d'historique), prochaine dans l'ordre de `PLAN.md`.

**Leçons retenues** : (1) un mécanisme qui "voit" un résultat d'outil terse
(confirmation d'action) sans jamais voir l'état réel qui en résulte juge
dans le vide — vrai pour la vérification ET pour la (re)planification,
trouvé deux fois séparément avant d'être corrigé aux deux endroits ; (2)
corriger un défaut d'ancrage peut en exposer un autre juste derrière (la
confusion de référence n'existait pas avant que le planificateur voie du
contenu réel) — chaque correctif de ce chantier a été vérifié par une sonde
live dédiée, jamais supposé correct depuis les tests unitaires seuls ; (3)
un faux négatif de mesure (le harnais lui-même) peut ressembler à une
régression de l'agent — le journal d'audit (`GET /audit?thread_id=`) a été
la seule façon de trancher entre les deux à chaque fois.

**Suite v2 (proposée, validée, fixtures pas encore construites)** : 8
tâches couvrant multi-sites/tâches longues, ambiguïté à résoudre, 2 pièges
à injection de prompt (préfiguration Phase 3 — échec attendu tant que
PromptGuard n'existe pas), et tâches à ENGAGEMENT réel (annulation,
suppression) pour exercer le pipeline de validation en conditions réelles.
Nouveau point zéro assumé, comparaisons v1/v2 interdites. Détail dans
`docs/briefs/phase-1-coeur-cognitif.md`.

### Constat post-action : historique et mécanisme actuel

Trois versions successives (voir docs/history.md, « correctif latence 1/2 »
puis « 1/2-bis » puis « 1/2-ter ») avant la version actuelle : un appel
LLM séparé (`verify_action`, coûteux) -> un marqueur texte
`[CONSTAT: ...]` dans la réponse du tour suivant (trop fragile, souvent
omis) -> un tool call dédié obligatoire `report_and_act` (fiabilité réelle
mesurée ~9%, le modèle ne coordonnait pas deux tool_calls dans le même
tour) -> **mécanisme actuel, fusionné** : `constat_precedent`
(`atteint`/`non_atteint`/`sans_objet`) est un paramètre REQUIS du schéma de
CHAQUE outil réel (`_inject_constat_param`, `app/graph.py`, gated sur
`VERIFICATION_ENABLED`) — un seul tool call porte à la fois l'action et
son constat. `report_and_act` reste l'outil de repli pour le seul cas
sans action réelle (réponse en texte pur). Dégradation INVERSÉE
(constat absent/mal formé -> `sans_objet`, budget de tentatives inchangé,
compté dans `constats_inexploitables` plutôt que facturé comme un échec)
et juge de COUVERTURE permanent (`verification_opportunities`/
`verification_exploitable`, journal d'audit `role="verification"`) —
compromis latence observé : ce schéma augmenté sur ~64 outils à chaque
tour a un coût de prompt mesurable (voir docs/history.md pour le détail
chiffré), chantier encore ouvert.

### Conscience temporelle (PLAN.md Phase 1, point 7)

Prévue dès la Phase 0 (sonde T11, « quelle est la dernière version stable
de Python ? ») mais jamais construite jusqu'au chantier latence — implémentée
après diagnostic direct de l'échec (le modèle décidait de vérifier via le
web mais interrogeait `browser_extract` avec un préfixe de version issu de
sa propre connaissance figée, ratant la version réellement affichée) :
- `_date_directive()` (`app/graph.py`) : injection de date à CHAQUE tour,
  granularité JOUR uniquement (jamais l'heure, pour préserver le cache de
  préfixe ExLlamaV3), positionnée en fin de bloc système statique. Fuseau
  `TZ` (`docker-compose.yml`, défaut `Europe/Paris`).
- `PEREMPTION_DIRECTIVE` : consigne de vérifier via le web tout fait
  volatil (versions, prix, actualité, rôles, état de services) plutôt que
  répondre de mémoire, **et** de ne jamais injecter une valeur déjà
  supposée dans la requête de vérification elle-même (cherche un terme
  neutre, pas un numéro de version précis qu'on suppose déjà) — sans quoi
  une page réelle mentionnant aussi d'anciennes valeurs (historique des
  releases) confirme le biais au lieu de le corriger.

Résultat mesuré : T11 3/3 sur campagne complète (0/3 sur les 3 campagnes
précédentes).

### Vérification en masse (`BULK_CHECK_DIRECTIVE`, mode bulk de `browser_extract`)

Trouvée en investiguant T1 (voir docs/history.md) : quand l'information
cherchée n'apparaît que sur les pages de détail (jamais le listing) et
qu'il faut en vérifier plusieurs, une navigation page par page épuise le
budget d'itérations avant même d'avoir tout vérifié — le modèle finissait
par deviner une URL (bloquée à raison par le garde-fou anti-fabrication).
Corrigé une première fois via `browser_evaluate` (boucle `fetch()` écrite
par le modèle, `TIER_SENSITIVE`/`NEVER_GRANTABLE`, voir
`approval_policy.py`) : fonctionnel (T1 3/3, 0/3 sur les campagnes
précédentes, 5-6 tool calls par run contre 20-30+ avant) mais fragile —
dépend du modèle pour écrire du JS correct à chaque fois, pour un besoin
qui n'a jamais requis de code arbitraire.

`browser_extract` (`services/mcp-client/app/main.py`) accepte désormais un
paramètre `urls` optionnel (mode bulk) : même template JS FIXE que la
recherche mono-page
(`fetch()` + `DOMParser` + le même parcours de nœuds texte, par URL),
`TIER_READ` — le modèle ne fournit que la liste d'URL, jamais de code.
Échec sur une URL individuelle (réseau, CORS cross-origin) capturé par
page, jamais propagé à tout le lot. `BULK_CHECK_DIRECTIVE` pointe
désormais vers ce paramètre plutôt que vers `browser_evaluate`.

### Outillage de campagne (`scripts/run-campaign.sh`)

Lance le harnais de bout en bout, zéro intervention entre le lancement et
le rapport : estimation de durée (médiane courante par tâche x tâches x
répétitions, voir `DURATION_ESTIMATE_CACHE.json` — un cache glissant
d'ESTIMATION, pas un historique, voir plus bas) -> préambule
(`campaign_preflight.run_preflight` : readiness LLM réelle — un appel de
complétion, pas un simple `/health` — PUIS schéma d'outils agent/mcp-client
synchronisés) -> campagne -> rapport écrit -> notification de fin (fichier
`.DONE` toujours ; `ntfy`/mail en plus si `NTFY_TOPIC`/`MAIL_TO` sont
définis).

**Persistance de campagne (`tests_integration/campaign_persistence.py`)** :
suite à un constat d'inventaire (voir docs/history.md, « INVENTAIRE DE
PERSISTANCE » puis « PERSISTANCE DES CAMPAGNES ») montrant que rien ne
survivait d'une campagne au-delà du Markdown prose, chaque campagne écrit
désormais `campaign-<timestamp>-<label>.json` (jamais réécrit ensuite) à
côté du rapport : métadonnées de contexte figées au lancement (commit git,
ID d'image des conteneurs `langgraph-agent`/`mcp-client`/`tabbyapi`/
`playwright-mcp`, modèle réellement chargé côté TabbyAPI via `GET
/v1/model`, flags d'env effectifs du conteneur `langgraph-agent`) + une
ligne par run (`thread_id` — clé de jointure directe avec
`/workspace/.audit`, aucun champ à ajouter côté `audit_log.py` — statut,
cause d'échec, tool_calls, couverture des constats, durée) + un échantillon
TabbyAPI BRUT par requête journalisée (pas seulement l'agrégat). `_write_report`
(le Markdown existant, format inchangé à l'œil) est désormais une VUE :
rendue depuis une relecture de ce JSON, jamais depuis les données en
mémoire directement — seule source de vérité.

Correction factuelle actée pendant ce chantier (CLAUDE.md #8) : TabbyAPI
(vérifié dans l'image `agentic-ai-playground-tabbyapi`,
`/app/endpoints/*/router.py`) n'expose PAS d'endpoint `/metrics`
Prometheus, contrairement à llama-server (voir Observabilité plus bas et
le commentaire déjà présent dans `docker-compose.yml`, service
`dashboard`) —
un relevé « avant/après » sur cet endpoint n'aurait rien pu récupérer. Les
échantillons persistés proviennent donc du texte des logs du conteneur
(regex sur "N tokens generated in ... Process: X cached tokens and Y new
tokens at Z T/s"), la seule source réelle de performance par requête
disponible — d'où aussi la config `logging` (max-size/max-file) ajoutée au
service `tabbyapi` dans `docker-compose.yml` : ces logs ne doivent plus
disparaître au gré d'un défaut de daemon Docker plus restrictif que prévu.

**Backfill borné** (`tests_integration/backfill_campaigns_index.py`) :
script ponctuel, exécuté une fois pour les campagnes antérieures à ce
mécanisme — reconstruit une fenêtre temporelle APPROXIMATIVE par campagne
depuis les dates déjà présentes dans les rapports Markdown/fichiers
`.DONE` (`campaigns-index.json`). Ne ressuscite aucune métrique perdue,
rend seulement `/workspace/.audit` (jamais purgé) navigable
rétroactivement par fenêtre de temps.

```
scripts/run-campaign.sh                      # campagne complète (11 tâches x 3)
scripts/run-campaign.sh --tasks T1,T7,T11    # smoke ciblé, itération rapide
scripts/run-campaign.sh --tasks T7 --reps 1  # smoke minimal
```

**Protocole** : le mode smoke (`--tasks`) sert à ITÉRER vite sur un
correctif — n réduit, pas de signification statistique pour arbitrer un
seuil de passage/régression. Seule la campagne complète (3 répétitions,
11 tâches) compte comme mesure de référence pour un checkpoint. Trouvé en
conditions réelles (voir docs/history.md, « outillage de campagne ») : la
readiness LLM a mordu une fois — `docker compose up --build` avait recréé
TabbyAPI en même temps qu'une campagne démarrait, qui a alors tourné
~20s trop tôt contre un serveur pas encore à l'écoute (30 échecs quasi
instantanés, aucune assertion pour le signaler) — d'où sa vérification
systématique en tête de préambule désormais.

## OCR d'appoint (`services/ocr-service`)

**Pourquoi** : le VLM servi par défaut (Qwen3.6 MoE) raisonne bien mais
localise mal — son grounding visuel (viser le bon pixel d'un élément à
l'écran) reste imprécis, sans OCR ni détection d'éléments UI dédiée (voir
Limites connues assumées plus bas). `ocr-service` compense en donnant à
l'agent des coordonnées de texte EXACTES via deux tools MCP : `find_text
(query, fuzzy=true)` (correspondances triées par confiance, liste vide si
aucune — jamais d'erreur) et `read_screen()` (tout le texte détecté,
plafonné à 80 éléments). Consigne de grounding injectée au system prompt de
langgraph-agent (`GROUNDING_DIRECTIVE`, `app/graph.py`) : privilégier
`find_text` à l'estimation visuelle pour cliquer sur du texte, réserver
cette dernière aux éléments sans texte (icônes).

Serveur MCP HTTP persistant (Streamable HTTP, bearer `OCR_AUTH_TOKEN`), sur
le même modèle que `desktop`/GhostDesk côté `mcp-client` — pas un conteneur
spawné à la demande. `find_text`/`read_screen` sont tier lecture
(`approval_policy.py`) : lecture pure, aucun effet de bord, auto-approuvés
et silencieux.

**Capture** : `ocr-service` se connecte lui-même en Streamable HTTP à
GhostDesk (réseau interne `agent-net`, bearer `GHOSTDESK_AUTH_TOKEN`,
`format="png"` explicite — aucune dépendance au décodage WebP natif de
llama-server, non pertinent ici) pour appeler `screen_shot` à chaque
`find_text`/`read_screen`. Aucune image ne transite par `mcp-client` ni par
le LLM pour ce flux, entièrement interne à `ocr-service`.

**Mapping de coordonnées — source classique de clics décalés** : PaddleOCR
travaille en pixels réels de la capture, alors que `mouse_click` côté
GhostDesk attend le repère normalisé 0-1000 (même repère que
`GHOSTDESK_MODEL_SPACE` côté `mcp-client`, voir Supervision humaine plus
bas). `ocr-service` convertit donc systématiquement ses coordonnées avant de
répondre (`x_norm = round(x_px * 1000 / largeur_image)`, voir
`app/coords.py`) — sans cette conversion, les coordonnées renvoyées par
`find_text` seraient en pixels alors que le modèle (et GhostDesk) les
interprètent en 0-1000, garantissant des clics à côté de leur cible.
`OCR_COORD_SPACE` (défaut `"1000"`) désactive cette conversion (`"pixels"`)
si l'appelant travaille lui-même en pixels.

**PaddleOCR** : PaddleOCR regroupe le français et l'anglais sous un seul
modèle de reconnaissance (alphabet latin partagé), inutile de faire tourner
deux passes OCR séparées pour ce projet. Modèles téléchargés **au build** de
l'image Docker (`ARG OCR_LANGS`, voir `services/ocr-service/Dockerfile`),
jamais au premier appel — évite un accès réseau et plusieurs secondes de
latence en production.

Hors périmètre explicite (itération future) : détection d'icônes/éléments UI
sans texte (type OmniParser), annotation Set-of-Marks des screenshots, OCR
GPU, cache des résultats entre appels.

