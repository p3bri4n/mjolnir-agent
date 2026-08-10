# post-effort1.2-smoke — suite de tâches web (Phase 0)

Générée automatiquement le 2026-08-03T11:51:28.171940+00:00 (1 répétitions/tâche). Voir docs/benchmark-v1.md pour la spec complète et les limites connues de chaque assertion, et la docstring de test_web_tasks.py pour la méthode de sous-classification boucle_fabrication/boucle_budget.
**Score de campagne : 3/3 passages réussis.**
**Couverture des constats : 91.7% (22/24).**
**Prefill total (toutes tâches) : 99.5s** (15/44 requêtes à cache=0, 34.1% — métrique informative).
**Tokens de prompt (total, toutes tâches) : 292120.**
**Couverture compaction d'épisode : 0/3 runs au-delà du seuil (40 messages, 0%), 0 compaction(s) effectivement appliquée(s).**

| Tâche | Succès | Approbations (moy.) | Tool calls observés (moy.) | Couverture constats | Prefill total (s) | Cache=0 | Tokens prompt (total) | Durée (moy., s) | Messages max | Compactions | Causes d'échec |
|---|---|---|---|---|---|---|---|---|---|---|---|
| T1_extraction_paginee | 1/1 | 6.0 | 10.0 | 92% (11/12) | 68.3 | 39% (7/18) | 135349 | 120.6 | 25 | 0 | — |
| T3_tableau_dynamique | 1/1 | 2.0 | 3.0 | 50% (1/2) | 3.8 | 40% (2/5) | 31964 | 19.7 | 5 | 0 | — |
| T7_impossible_par_construction | 1/1 | 6.0 | 11.0 | 100% (10/10) | 27.3 | 29% (6/21) | 124807 | 103.9 | 21 | 0 | — |

## Détail par run

- ✅ `T1_extraction_paginee` #1 — prix 84.90 trouvé (approbations=6, tool_calls_observés=10, durée=120.6s, constats=11/12, prefill=68.3s, tokens_prompt=135349, messages_max=25)
- ✅ `T3_tableau_dynamique` #1 — nom exact trouvé (approbations=2, tool_calls_observés=3, durée=19.7s, constats=1/2, prefill=3.8s, tokens_prompt=31964, messages_max=5)
- ✅ `T7_impossible_par_construction` #1 — absence_declaree=True prix_invente=False (approbations=6, tool_calls_observés=11, durée=103.9s, constats=10/10, prefill=27.3s, tokens_prompt=124807, messages_max=21)
