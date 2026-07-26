# État d'avancement

Change à chaque checkpoint — voir `PLAN.md` pour la feuille de route (change
rarement, source de vérité en cas de divergence) et `docs/history.md` pour le
détail chronologique complet.

## Phase 0 — harnais de niveau tâche

Livré : `tests_integration/test_web_tasks.py`, 11 tâches (`docs/benchmark-v1.md`),
critères programmatiques, métriques par run, baseline consignée.

## Phase 1 — boucle plan → agir → vérifier → replanifier

**1ère tranche** (garde-fou fabrication d'URL + tronquage snapshots) :
Campagne A budget 20, score global 16/33 → 24/33 — aucun des 5 critères de
réussite fixés au checkpoint n'était intégralement atteint (voir docs/history.md).

**Campagne A finale** (isolation entre tâches + `browser_extract`) : 30/33.

**« Cœur cognitif »** (les 7 points de la Phase 1, séquencés itération par
itération — voir `docs/briefs/phase-1-coeur-cognitif.md`) : livré et mesuré.
Campagne finale (4 mécanismes actifs) : 29/33, cohérent avec la Campagne A
(30/33, pas une régression) — détail tâche par tâche dans docs/history.md et
`tests_integration/TASKS-BASELINE-post-coeur-cognitif.md`. Backlog T1/T7/T9
investigué et clos (voir docs/history.md, « INVENTAIRE DE PERSISTANCE » et les
investigations T7/T9).

**Suivi de ce lot** (voir docs/history.md) :
- Persistance de campagne (`campaign_persistence.py`) : JSON par run,
  `thread_id`, échantillons TabbyAPI bruts — livré.
- Flags du cœur cognitif : défauts inversés à `true` (mesuré et adopté),
  garde-fou de préambule (`check_agent_flags`) — livré.
- Restructuration + anglais (`docs/briefs/restructuration-et-anglais.md`) :
  phase 0 (contrats) livrée, phase 1 (allègement) faite sur `graph.py`
  seul (reste des services non traité), phase 3 (découpage README) en
  cours.

**Non traité de ce lot** (prérequis du chantier restructuration) : angle
mort d'audit (le tout premier appel de chaque outil par thread est invisible
dans `/audit`, voir docs/history.md), mode bulk de `browser_extract`, campagne
complète 33 runs de clôture.

## Phases 2 à 4

Non démarrées (discipline de contexte, tiers de sécurité par nature
d'action, consolidation — voir `PLAN.md`).

## Chantier différé : dossier Mjolnir (second modèle)

Non démarré — voir `PLAN.md`.
