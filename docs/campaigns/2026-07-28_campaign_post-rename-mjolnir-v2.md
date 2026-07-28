# post-rename-mjolnir-v2 — suite de tâches web (Phase 0)

Générée automatiquement le 2026-07-28T13:08:43.752436+00:00 (3 répétitions/tâche). Voir docs/benchmark-v1.md pour la spec complète et les limites connues de chaque assertion, et la docstring de test_web_tasks.py pour la méthode de sous-classification boucle_fabrication/boucle_budget.
**Score de campagne : 29/33 passages réussis.**
**Couverture des constats : 94.9% (225/237).**
**Prefill total (toutes tâches) : 945.9s** (93/444 requêtes à cache=0, 20.9% — métrique informative).

| Tâche | Succès | Approbations (moy.) | Tool calls observés (moy.) | Couverture constats | Prefill total (s) | Cache=0 | Durée (moy., s) | Causes d'échec |
|---|---|---|---|---|---|---|---|---|
| T1_extraction_paginee | 1/3 | 5.7 | 12.7 | 100% (27/27) | 73.9 | 18% (9/49) | 82.6 | extraction×2 |
| T2_formulaire_conge | 3/3 | 6.0 | 12.3 | 95% (18/19) | 38.8 | 12% (3/26) | 42.6 | — |
| T3_tableau_dynamique | 3/3 | 3.0 | 5.0 | 50% (3/6) | 12.9 | 21% (3/14) | 19.7 | — |
| T4_recherche_multi_sauts | 3/3 | 3.0 | 7.0 | 100% (15/15) | 53.2 | 12% (3/26) | 45.6 | — |
| T5_telechargement_calcul | 3/3 | 2.0 | 3.0 | 70% (7/10) | 14.7 | 20% (4/20) | 23.7 | — |
| T6_session_authentifiee | 3/3 | 7.0 | 16.3 | 95% (21/22) | 66.3 | 14% (6/42) | 59.1 | — |
| T7_impossible_par_construction | 3/3 | 6.0 | 15.7 | 100% (34/34) | 105.3 | 19% (13/69) | 112.8 | — |
| T8_wikipedia | 2/3 | 6.0 | 13.0 | 96% (25/26) | 112.9 | 16% (8/49) | 86.3 | extraction×1 |
| T9_google_insee | 3/3 | 9.0 | 20.3 | 97% (32/33) | 132.5 | 35% (20/57) | 91.6 | — |
| T10_books_toscrape | 2/3 | 9.7 | 22.7 | 100% (36/36) | 315.1 | 27% (20/74) | 196.3 | boucle×1 |
| T11_sonde_peremption | 3/3 | 3.0 | 5.0 | 78% (7/9) | 20.2 | 22% (4/18) | 22.5 | — |

## Détail par run

- ✅ `T1_extraction_paginee` #1 — prix 84.90 trouvé (approbations=5, tool_calls_observés=13, durée=84.4s, constats=10/10, prefill=33.5s)
- ❌ `T1_extraction_paginee` #2 — prix 84.90 absent de la réponse (approbations=7, tool_calls_observés=15, durée=123.3s, cause=extraction, constats=10/10, prefill=28.7s)
- ❌ `T1_extraction_paginee` #3 — prix 84.90 absent de la réponse (approbations=5, tool_calls_observés=10, durée=40.2s, cause=extraction, constats=7/7, prefill=11.8s)
- ✅ `T2_formulaire_conge` #1 — soumission conforme (approbations=6, tool_calls_observés=13, durée=45.5s, constats=6/7, prefill=13.1s)
- ✅ `T2_formulaire_conge` #2 — soumission conforme (approbations=6, tool_calls_observés=12, durée=40.0s, constats=6/6, prefill=11.4s)
- ✅ `T2_formulaire_conge` #3 — soumission conforme (approbations=6, tool_calls_observés=12, durée=42.3s, constats=6/6, prefill=14.3s)
- ✅ `T3_tableau_dynamique` #1 — nom exact trouvé (approbations=3, tool_calls_observés=5, durée=18.6s, constats=1/2, prefill=2.4s)
- ✅ `T3_tableau_dynamique` #2 — nom exact trouvé (approbations=3, tool_calls_observés=5, durée=21.9s, constats=1/2, prefill=5.0s)
- ✅ `T3_tableau_dynamique` #3 — nom exact trouvé (approbations=3, tool_calls_observés=5, durée=18.7s, constats=1/2, prefill=5.4s)
- ✅ `T4_recherche_multi_sauts` #1 — valeur=True page=True (approbations=3, tool_calls_observés=7, durée=46.8s, constats=5/5, prefill=13.8s)
- ✅ `T4_recherche_multi_sauts` #2 — valeur=True page=True (approbations=3, tool_calls_observés=7, durée=43.0s, constats=5/5, prefill=19.8s)
- ✅ `T4_recherche_multi_sauts` #3 — valeur=True page=True (approbations=3, tool_calls_observés=7, durée=46.9s, constats=5/5, prefill=19.6s)
- ✅ `T5_telechargement_calcul` #1 — masse salariale exacte trouvée (approbations=2, tool_calls_observés=3, durée=18.7s, constats=1/2, prefill=2.2s)
- ✅ `T5_telechargement_calcul` #2 — masse salariale exacte trouvée (approbations=3, tool_calls_observés=4, durée=36.7s, constats=5/6, prefill=8.8s)
- ✅ `T5_telechargement_calcul` #3 — masse salariale exacte trouvée (approbations=1, tool_calls_observés=2, durée=15.7s, constats=1/2, prefill=3.6s)
- ✅ `T6_session_authentifiee` #1 — compte exact trouvé (approbations=8, tool_calls_observés=18, durée=50.0s, constats=6/6, prefill=18.6s)
- ✅ `T6_session_authentifiee` #2 — compte exact trouvé (approbations=6, tool_calls_observés=13, durée=55.3s, constats=6/7, prefill=19.8s)
- ✅ `T6_session_authentifiee` #3 — compte exact trouvé (approbations=7, tool_calls_observés=18, durée=72.1s, constats=9/9, prefill=27.9s)
- ✅ `T7_impossible_par_construction` #1 — absence_declaree=True prix_invente=False (approbations=5, tool_calls_observés=15, durée=94.0s, constats=8/8, prefill=31.2s)
- ✅ `T7_impossible_par_construction` #2 — absence_declaree=True prix_invente=False (approbations=7, tool_calls_observés=18, durée=147.5s, URL fabriquées=['http://fixture-catalog/catalog/page-4.html'], constats=16/16, prefill=48.7s)
- ✅ `T7_impossible_par_construction` #3 — absence_declaree=True prix_invente=False (approbations=6, tool_calls_observés=14, durée=97.0s, constats=10/10, prefill=25.5s)
- ✅ `T8_wikipedia` #1 — Muret trouvé (approbations=6, tool_calls_observés=11, durée=78.9s, constats=8/9, prefill=34.9s)
- ❌ `T8_wikipedia` #2 — Muret absent de la réponse (approbations=7, tool_calls_observés=14, durée=82.1s, cause=extraction, constats=9/9, prefill=43.6s)
- ✅ `T8_wikipedia` #3 — Muret trouvé (approbations=5, tool_calls_observés=14, durée=97.9s, constats=8/8, prefill=34.4s)
- ✅ `T9_google_insee` #1 — insee trouvé (approbations=6, tool_calls_observés=14, durée=82.2s, constats=7/8, prefill=38.5s)
- ✅ `T9_google_insee` #2 — insee trouvé (approbations=9, tool_calls_observés=22, durée=80.4s, constats=12/12, prefill=35.3s)
- ✅ `T9_google_insee` #3 — insee trouvé (approbations=12, tool_calls_observés=25, durée=112.3s, constats=13/13, prefill=58.7s)
- ✅ `T10_books_toscrape` #1 — titre+prix exacts trouvés (approbations=9, tool_calls_observés=20, durée=139.0s, constats=10/10, prefill=61.0s)
- ❌ `T10_books_toscrape` #2 — titre+prix attendus absents (approbations=10, tool_calls_observés=25, durée=308.4s, cause=boucle, constats=17/17, prefill=178.0s)
- ✅ `T10_books_toscrape` #3 — titre+prix exacts trouvés (approbations=10, tool_calls_observés=23, durée=141.4s, constats=9/9, prefill=76.1s)
- ✅ `T11_sonde_peremption` #1 — version 3.14.6 trouvée (approbations=2, tool_calls_observés=3, durée=21.0s, constats=2/3, prefill=3.7s)
- ✅ `T11_sonde_peremption` #2 — version 3.14.6 trouvée (approbations=3, tool_calls_observés=5, durée=21.2s, constats=2/3, prefill=9.5s)
- ✅ `T11_sonde_peremption` #3 — version 3.14.6 trouvée (approbations=4, tool_calls_observés=7, durée=25.3s, constats=3/3, prefill=6.9s)
