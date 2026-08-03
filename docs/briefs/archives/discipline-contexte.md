# Phase 2 — Discipline de contexte (PLAN.md)

**Statut : clos.** Brief écrit rétroactivement (écart au « brief avant le
code », assumé — voir ci-dessous) pour consigner le raisonnement de
clôture, comme demandé explicitement par l'utilisateur (point 4 de la
demande de requalification du 2026-07-28).

## Point 1 — Rétention d'images

Déjà livré avant cet effort (`MAX_IMAGES_IN_CONTEXT`, `_apply_image_retention`,
`app/graph.py`), dans le cadre du lot cœur cognitif. Non retouché ici.

## Point 2 — Compaction d'épisode

### Livré

- `_apply_episode_compaction`/`_summarize_subtask` (`app/graph.py`) : filtre
  transitoire (jamais de mutation du checkpointer, même principe que la
  rétention d'images) remplaçant les tours bruts d'une sous-tâche terminée
  par un résumé structuré, au-delà d'`EPISODE_COMPACTION_TURN_THRESHOLD`
  messages.
- État léger `subtask_message_start` (bornes de messages par sous-tâche),
  peuplé par `plan_task`/`revise_plan`/`verify_action`/`replan_task`,
  dégradant silencieusement (jamais de raise) sur toute désynchro
  plan/bornes.
- `EPISODE_COMPACTION_ENABLED` par défaut `false` — livré éteint, comme
  `PLANNER_ENABLED` à l'origine.
- Compteurs de couverture permanents (`episode_compaction_messages_max`/
  `episode_compaction_applied_count`), journalisés à CHAQUE `call_llm`,
  flag actif ou non — nouvelle règle ajoutée à `CLAUDE.md` (mesure) :
  « un mécanisme conditionnel se livre avec son compteur de
  déclenchement ».
- Juge tokens/tâche réel (`prompt_tokens_total`,
  `campaign_persistence.aggregate_prefill_stats`) : somme
  `cached_tokens`+`new_tokens` par appel TabbyAPI, distinct du prefill en
  secondes qui mélange volume et taux de cache.

### Mesuré, puis requalifié

Campagne complète 33 runs du 2026-07-28 (`EPISODE_COMPACTION_ENABLED=true`
forcé pour l'expérience) : 30/33, cohérent avec la baseline 29/33.
**Requalifiée « NON CONCLUANTE »** une fois le compteur de couverture
appliqué rétroactivement (reconstruction par proxy, le compteur réel
n'existait pas encore à l'exécution) : seulement **9-15% des runs**
franchissaient le seuil de 40 messages. Sous toute barre de couverture
raisonnable, cette campagne mesurait le bruit des runs non concernés, pas
l'effet du mécanisme. Le delta de cache=0 observé (amélioration) est en
plus incompatible dans son SENS avec la compaction comme cause : réécrire
le préfixe du prompt devrait dégrader le cache hit, pas l'améliorer — la
direction même de l'écart pointe vers du bruit.
Voir `docs/campaigns/2026-07-28_campaign_episode-compaction-enabled.md`
et `docs/project-status.md`.

### Test ciblé (hors benchmark gelé) — tenté, non concluant pour une
### raison structurelle plus profonde

Tentative de construire 1-2 tâches locales garantissant >60 messages par
construction, flag OFF puis ON, 3 répétitions (`tests_integration/
probe_episode_compaction.py`, jamais ajouté à la suite gelée). Deux
designs essayés :

1. **12 puis 8 soumissions de formulaire de congé séquentielles**
   (fixture-hr-app) : bloqué par `_PLAN_SUBTASKS_MAX=8` (12 sous-tâches
   rejetées en boucle, contexte explosé à 671713 tokens avant échec
   propre — bug de conception de ma tâche, pas du mécanisme), puis par la
   friction réelle de `browser_fill_form` sur les sélecteurs (le modèle
   tâtonne plusieurs tours avant de trouver le bon format `ref=eXX`).
2. **30 fiches produit du catalogue, lecture seule** (pas de friction de
   formulaire) : bute sur une friction DIFFÉRENTE et plus structurelle —
   le juge de vérification post-action (`verify_action`) compare le
   critère de succès à un SNAPSHOT DE PAGE ; un critère du type
   « extraire le prix et l'ajouter à la liste » n'est pas observable sur
   la page (rien n'y indique qu'une valeur a été « ajoutée à une liste »
   interne au raisonnement du modèle), donc le juge répond
   systématiquement `non_atteint` même quand l'action a réellement
   réussi. `SUBTASK_ATTEMPT_BUDGET=3` × `REPLAN_BUDGET=2` s'épuise après
   ~17 tool_calls, à l'identique sur les 3 répétitions (comportement
   reproductible, pas du bruit d'échantillon) — la tâche abandonne
   toujours avant d'approcher le seuil de 40 messages.

**Conclusion consignée** : avec `PLANNER_ENABLED`+`VERIFICATION_ENABLED`
actifs — nécessaires pour que la compaction ait des sous-tâches
« fait »/« echoue » à compacter — le pipeline plan→act→verify→replan
lui-même limite structurellement la longueur des épisodes single-task :
`_PLAN_SUBTASKS_MAX=8` borne la granularité, et le juge de vérification
page-observable échoue sur tout critère de succès non visible à l'écran.
Ce n'est pas seulement un problème d'échantillonnage du benchmark actuel
(9-15% de couverture sur 33 runs) : c'est cohérent avec un phénomène plus
large — les épisodes longs à sous-tâches nombreuses sont rares PAR
CONSTRUCTION de l'architecture actuelle, pas seulement sous-représentés
dans les 11 tâches gelées.

## Point 3 — Mesure tokens/tâche

Livré (voir point 2, juge tokens/tâche réel) — reste à utiliser dans une
vraie campagne de répétitions quand l'occasion se présentera (benchmark
v2).

## Décision du flag

**`EPISODE_COMPACTION_ENABLED` reste `false`.** Réévaluation à l'arrivée
du benchmark v2, dont les tâches longues (si elles existent — reste à
concevoir en tenant compte de la friction structurelle ci-dessus,
probablement avec des critères de succès délibérément page-observables,
ou un mécanisme de vérification moins strict pour les tâches à haut
volume de sous-tâches) exerceront le mécanisme naturellement, sans
artifice.

**Ce n'est pas un échec.** Le mécanisme est construit, testé
unitairement (11 tests dédiés), correctement gated derrière un flag
éteint par défaut, et le vrai obstacle à sa validation n'est pas dans le
mécanisme lui-même mais dans la rareté structurelle des épisodes assez
longs pour l'exercer avec l'architecture de vérification actuelle — un
diagnostic utile en soi pour la conception du benchmark v2. Mécanisme
construit en avance sur son besoin, conservé, validation différée.
