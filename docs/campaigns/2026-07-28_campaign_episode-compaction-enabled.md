**REQUALIFIÉ « non concluant » (analyse d'archives a posteriori, zéro
run).** Ce rapport n'a pas encore le compteur de couverture permanent
(`episode_compaction_messages_max`/`episode_compaction_applied_count`,
ajouté après coup à `test_web_tasks.py`/`app/graph.py`). Reconstruction
par proxy depuis `tabbyapi_requests`/`tool_calls_observed` du JSON
(`campaign-20260728T130843Z-post-rename-mjolnir-v2.json` pour la
baseline, `campaign-20260728T141444Z-episode-compaction-enabled.json`
ici — `messages ≈ 2×tool_calls_observed + 2`) : seuls **3/33 runs (9%)**
ici et **5/33 (15%)** côté baseline sont estimés au-delà des 40 messages
d'`EPISODE_COMPACTION_TURN_THRESHOLD`. Sous les 30% requis pour que la
campagne teste réellement le mécanisme, le score 30/33 et l'écart de
prefill/cache observés avec la baseline (715.7s/16.7% ici vs 945.9s/20.9%)
sont du bruit de mesure, pas un effet du flag — à refaire avec le
compteur permanent avant toute conclusion, idéalement sur des tâches
conçues pour dépasser le seuil.

# episode-compaction-enabled — suite de tâches web (Phase 0)

Générée automatiquement le 2026-07-28T14:14:44.346219+00:00 (3 répétitions/tâche). Voir docs/benchmark-v1.md pour la spec complète et les limites connues de chaque assertion, et la docstring de test_web_tasks.py pour la méthode de sous-classification boucle_fabrication/boucle_budget.
**Score de campagne : 30/33 passages réussis.**
**Couverture des constats : 92.3% (180/195).**
**Prefill total (toutes tâches) : 715.7s** (64/383 requêtes à cache=0, 16.7% — métrique informative).

| Tâche | Succès | Approbations (moy.) | Tool calls observés (moy.) | Couverture constats | Prefill total (s) | Cache=0 | Durée (moy., s) | Causes d'échec |
|---|---|---|---|---|---|---|---|---|
| T1_extraction_paginee | 1/3 | 5.3 | 12.0 | 100% (22/22) | 51.3 | 15% (6/39) | 56.9 | extraction×2 |
| T2_formulaire_conge | 3/3 | 5.7 | 11.7 | 93% (14/15) | 32.9 | 12% (3/26) | 35.5 | — |
| T3_tableau_dynamique | 3/3 | 3.7 | 6.7 | 71% (5/7) | 19.3 | 19% (3/16) | 24.1 | — |
| T4_recherche_multi_sauts | 3/3 | 4.0 | 8.0 | 85% (17/20) | 47.7 | 24% (9/38) | 52.3 | — |
| T5_telechargement_calcul | 3/3 | 2.0 | 3.0 | 50% (3/6) | 11.5 | 19% (3/16) | 18.4 | — |
| T6_session_authentifiee | 3/3 | 5.7 | 13.3 | 91% (21/23) | 42.6 | 12% (4/33) | 43.4 | — |
| T7_impossible_par_construction | 2/3 | 6.7 | 18.0 | 100% (28/28) | 119.3 | 11% (7/62) | 108.2 | boucle_fabrication×1 |
| T8_wikipedia | 3/3 | 4.7 | 11.7 | 100% (21/21) | 96.6 | 14% (6/43) | 85.0 | — |
| T9_google_insee | 3/3 | 6.0 | 12.0 | 88% (15/17) | 65.0 | 24% (8/33) | 45.7 | — |
| T10_books_toscrape | 3/3 | 8.3 | 18.0 | 100% (26/26) | 208.2 | 19% (11/57) | 152.6 | — |
| T11_sonde_peremption | 3/3 | 2.7 | 4.3 | 80% (8/10) | 21.2 | 20% (4/20) | 23.0 | — |

## Détail par run

- ❌ `T1_extraction_paginee` #1 — prix 84.90 absent de la réponse (approbations=5, tool_calls_observés=10, durée=43.2s, cause=extraction, constats=7/7, prefill=10.3s)
- ✅ `T1_extraction_paginee` #2 — prix 84.90 trouvé (approbations=6, tool_calls_observés=16, durée=84.4s, constats=9/9, prefill=30.0s)
- ❌ `T1_extraction_paginee` #3 — prix 84.90 absent de la réponse (approbations=5, tool_calls_observés=10, durée=43.0s, cause=extraction, constats=6/6, prefill=10.9s)
- ✅ `T2_formulaire_conge` #1 — soumission conforme (approbations=6, tool_calls_observés=12, durée=31.4s, constats=3/3, prefill=8.6s)
- ✅ `T2_formulaire_conge` #2 — soumission conforme (approbations=6, tool_calls_observés=13, durée=42.2s, constats=6/7, prefill=12.8s)
- ✅ `T2_formulaire_conge` #3 — soumission conforme (approbations=5, tool_calls_observés=10, durée=32.8s, constats=5/5, prefill=11.5s)
- ✅ `T3_tableau_dynamique` #1 — nom exact trouvé (approbations=5, tool_calls_observés=10, durée=33.2s, constats=3/3, prefill=14.6s)
- ✅ `T3_tableau_dynamique` #2 — nom exact trouvé (approbations=3, tool_calls_observés=5, durée=18.5s, constats=1/2, prefill=2.4s)
- ✅ `T3_tableau_dynamique` #3 — nom exact trouvé (approbations=3, tool_calls_observés=5, durée=20.7s, constats=1/2, prefill=2.4s)
- ✅ `T4_recherche_multi_sauts` #1 — valeur=True page=True (approbations=4, tool_calls_observés=8, durée=44.6s, constats=5/6, prefill=15.2s)
- ✅ `T4_recherche_multi_sauts` #2 — valeur=True page=True (approbations=4, tool_calls_observés=8, durée=69.8s, constats=6/7, prefill=21.9s)
- ✅ `T4_recherche_multi_sauts` #3 — valeur=True page=True (approbations=4, tool_calls_observés=8, durée=42.4s, constats=6/7, prefill=10.7s)
- ✅ `T5_telechargement_calcul` #1 — masse salariale exacte trouvée (approbations=2, tool_calls_observés=3, durée=18.5s, constats=1/2, prefill=3.4s)
- ✅ `T5_telechargement_calcul` #2 — masse salariale exacte trouvée (approbations=2, tool_calls_observés=3, durée=18.4s, constats=1/2, prefill=3.4s)
- ✅ `T5_telechargement_calcul` #3 — masse salariale exacte trouvée (approbations=2, tool_calls_observés=3, durée=18.4s, constats=1/2, prefill=4.7s)
- ✅ `T6_session_authentifiee` #1 — compte exact trouvé (approbations=5, tool_calls_observés=12, durée=38.5s, constats=6/7, prefill=13.4s)
- ✅ `T6_session_authentifiee` #2 — compte exact trouvé (approbations=7, tool_calls_observés=17, durée=59.4s, constats=9/10, prefill=16.9s)
- ✅ `T6_session_authentifiee` #3 — compte exact trouvé (approbations=5, tool_calls_observés=11, durée=32.2s, constats=6/6, prefill=12.3s)
- ✅ `T7_impossible_par_construction` #1 — absence_declaree=True prix_invente=False (approbations=6, tool_calls_observés=16, durée=123.3s, URL fabriquées=['http://fixture-catalog/catalog/page-10.html', 'http://fixture-catalog/catalog/page-9.html', 'http://fixture-catalog/catalog/page-8.html', 'http://fixture-catalog/catalog/page-7.html'], constats=13/13, prefill=41.5s)
- ✅ `T7_impossible_par_construction` #2 — absence_declaree=True prix_invente=False (approbations=5, tool_calls_observés=13, durée=75.8s, constats=10/10, prefill=33.6s)
- ❌ `T7_impossible_par_construction` #3 — absence_declaree=False prix_invente=False (approbations=9, tool_calls_observés=25, durée=125.5s, cause=boucle_fabrication, URL fabriquées=['http://fixture-catalog/catalog/page-4.html', 'http://fixture-catalog/catalog/product-31.html'], constats=5/5, prefill=44.2s)
- ✅ `T8_wikipedia` #1 — Muret trouvé (approbations=5, tool_calls_observés=12, durée=92.4s, constats=8/8, prefill=29.3s)
- ✅ `T8_wikipedia` #2 — Muret trouvé (approbations=4, tool_calls_observés=10, durée=73.6s, constats=6/6, prefill=32.1s)
- ✅ `T8_wikipedia` #3 — Muret trouvé (approbations=5, tool_calls_observés=13, durée=89.1s, constats=7/7, prefill=35.2s)
- ✅ `T9_google_insee` #1 — insee trouvé (approbations=5, tool_calls_observés=9, durée=42.3s, constats=3/4, prefill=26.8s)
- ✅ `T9_google_insee` #2 — insee trouvé (approbations=8, tool_calls_observés=18, durée=58.1s, constats=9/9, prefill=26.4s)
- ✅ `T9_google_insee` #3 — insee trouvé (approbations=5, tool_calls_observés=9, durée=36.6s, constats=3/4, prefill=11.8s)
- ✅ `T10_books_toscrape` #1 — titre+prix exacts trouvés (approbations=10, tool_calls_observés=20, durée=143.6s, constats=9/9, prefill=58.7s)
- ✅ `T10_books_toscrape` #2 — titre+prix exacts trouvés (approbations=9, tool_calls_observés=21, durée=240.2s, constats=13/13, prefill=115.2s)
- ✅ `T10_books_toscrape` #3 — titre+prix exacts trouvés (approbations=6, tool_calls_observés=13, durée=74.0s, constats=4/4, prefill=34.3s)
- ✅ `T11_sonde_peremption` #1 — version 3.14.6 trouvée (approbations=3, tool_calls_observés=5, durée=28.0s, constats=3/3, prefill=6.8s)
- ✅ `T11_sonde_peremption` #2 — version 3.14.6 trouvée (approbations=3, tool_calls_observés=5, durée=20.6s, constats=2/3, prefill=6.8s)
- ✅ `T11_sonde_peremption` #3 — version 3.14.6 trouvée (approbations=2, tool_calls_observés=3, durée=20.4s, constats=3/4, prefill=7.6s)
