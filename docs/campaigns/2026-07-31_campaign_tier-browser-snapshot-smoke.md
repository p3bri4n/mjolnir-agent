# tier-browser-snapshot-smoke — suite de tâches web (Phase 0)

Générée automatiquement le 2026-07-31T13:27:26.040173+00:00 (1 répétitions/tâche). Voir docs/benchmark-v1.md pour la spec complète et les limites connues de chaque assertion, et la docstring de test_web_tasks.py pour la méthode de sous-classification boucle_fabrication/boucle_budget.
**Score de campagne : 1/1 passages réussis.**
**Couverture des constats : 100.0% (11/11).**
**Prefill total (toutes tâches) : 45.6s** (4/20 requêtes à cache=0, 20.0% — métrique informative).
**Tokens de prompt (total, toutes tâches) : 243716.**
**Couverture compaction d'épisode : 0/1 runs au-delà du seuil (40 messages, 0%), 0 compaction(s) effectivement appliquée(s).**

| Tâche | Succès | Approbations (moy.) | Tool calls observés (moy.) | Couverture constats | Prefill total (s) | Cache=0 | Tokens prompt (total) | Durée (moy., s) | Messages max | Compactions | Causes d'échec |
|---|---|---|---|---|---|---|---|---|---|---|---|
| T1_extraction_paginee | 1/1 | 5.0 | 10.0 | 100% (11/11) | 45.6 | 20% (4/20) | 243716 | 102.1 | 29 | 0 | — |

## Détail par run

- ✅ `T1_extraction_paginee` #1 — prix 84.90 trouvé (approbations=5, tool_calls_observés=10, durée=102.1s, constats=11/11, prefill=45.6s, tokens_prompt=243716, messages_max=29)
