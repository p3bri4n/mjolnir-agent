# Campagne A (budget par défaut) — suite de tâches web (Phase 0)

Générée automatiquement le 2026-08-10T13:06:55.119772+00:00 (3 répétitions/tâche). Voir docs/benchmark-v1.md pour la spec complète et les limites connues de chaque assertion, et la docstring de test_web_tasks.py pour la méthode de sous-classification boucle_fabrication/boucle_budget.
**Score de campagne : 31/33 passages réussis.**
**Couverture des constats : 91.4% (181/198).**
**Prefill total (toutes tâches) : 932.3s** (94/399 requêtes à cache=0, 23.6% — métrique informative).
**Tokens de prompt (total, toutes tâches) : 3290731.**
**Couverture compaction d'épisode : 1/33 runs au-delà du seuil (40 messages, 3%), 0 compaction(s) effectivement appliquée(s).**

| Tâche | Succès | Approbations (moy.) | Tool calls observés (moy.) | Couverture constats | Prefill total (s) | Cache=0 | Tokens prompt (total) | Durée (moy., s) | Messages max | Compactions | Causes d'échec |
|---|---|---|---|---|---|---|---|---|---|---|---|
| T1_extraction_paginee | 3/3 | 4.3 | 8.0 | 100% (27/27) | 156.0 | 29% (13/45) | 378763 | 99.8 | 25 | 0 | — |
| T2_formulaire_conge | 3/3 | 4.7 | 8.3 | 82% (14/17) | 38.5 | 24% (6/25) | 183927 | 38.0 | 13 | 0 | — |
| T3_tableau_dynamique | 3/3 | 2.0 | 3.0 | 50% (3/6) | 10.3 | 50% (6/12) | 53158 | 20.4 | 5 | 0 | — |
| T4_recherche_multi_sauts | 2/3 | 2.0 | 3.3 | 83% (10/12) | 46.9 | 29% (6/21) | 160494 | 37.8 | 12 | 0 | extraction×1 |
| T5_telechargement_calcul | 3/3 | 2.0 | 3.0 | 62% (5/8) | 12.9 | 29% (5/17) | 103323 | 19.0 | 9 | 0 | — |
| T6_session_authentifiee | 3/3 | 4.0 | 7.0 | 85% (11/13) | 26.6 | 12% (3/24) | 166732 | 26.2 | 11 | 0 | — |
| T7_impossible_par_construction | 3/3 | 5.7 | 12.0 | 93% (26/28) | 101.7 | 17% (12/70) | 584395 | 121.6 | 41 | 0 | — |
| T8_wikipedia | 2/3 | 4.3 | 10.0 | 100% (19/19) | 102.0 | 7% (3/44) | 508713 | 77.3 | 27 | 0 | infra×1 |
| T9_google_insee | 3/3 | 6.7 | 13.0 | 100% (24/24) | 136.5 | 27% (14/52) | 366991 | 99.9 | 19 | 0 | — |
| T10_books_toscrape | 3/3 | 9.7 | 19.7 | 100% (36/36) | 281.0 | 31% (22/72) | 663999 | 193.1 | 37 | 0 | — |
| T11_sonde_peremption | 3/3 | 2.0 | 3.0 | 75% (6/8) | 19.8 | 24% (4/17) | 120236 | 18.9 | 7 | 0 | — |

## Détail par run

- ✅ `T1_extraction_paginee` #1 — prix 84.90 trouvé (approbations=5, tool_calls_observés=10, durée=135.8s, constats=11/11, prefill=71.2s, tokens_prompt=137901, messages_max=25)
- ✅ `T1_extraction_paginee` #2 — prix 84.90 trouvé (approbations=4, tool_calls_observés=8, durée=100.7s, constats=9/9, prefill=60.5s, tokens_prompt=138545, messages_max=23)
- ✅ `T1_extraction_paginee` #3 — prix 84.90 trouvé (approbations=4, tool_calls_observés=6, durée=63.0s, constats=7/7, prefill=24.4s, tokens_prompt=102317, messages_max=15)
- ✅ `T2_formulaire_conge` #1 — soumission conforme (approbations=5, tool_calls_observés=9, durée=38.5s, constats=5/6, prefill=11.5s, tokens_prompt=57384, messages_max=13)
- ✅ `T2_formulaire_conge` #2 — soumission conforme (approbations=5, tool_calls_observés=9, durée=40.8s, constats=5/6, prefill=15.1s, tokens_prompt=68588, messages_max=13)
- ✅ `T2_formulaire_conge` #3 — soumission conforme (approbations=4, tool_calls_observés=7, durée=34.7s, constats=4/5, prefill=11.9s, tokens_prompt=57955, messages_max=11)
- ✅ `T3_tableau_dynamique` #1 — nom exact trouvé (approbations=2, tool_calls_observés=3, durée=22.5s, constats=1/2, prefill=5.1s, tokens_prompt=17755, messages_max=5)
- ✅ `T3_tableau_dynamique` #2 — nom exact trouvé (approbations=2, tool_calls_observés=3, durée=19.6s, constats=1/2, prefill=2.6s, tokens_prompt=17706, messages_max=5)
- ✅ `T3_tableau_dynamique` #3 — nom exact trouvé (approbations=2, tool_calls_observés=3, durée=19.1s, constats=1/2, prefill=2.6s, tokens_prompt=17697, messages_max=5)
- ❌ `T4_recherche_multi_sauts` #1 — valeur=True page=False (approbations=2, tool_calls_observés=4, durée=53.2s, cause=extraction, constats=4/5, prefill=19.8s, tokens_prompt=65112, messages_max=12)
- ✅ `T4_recherche_multi_sauts` #2 — valeur=True page=True (approbations=2, tool_calls_observés=3, durée=27.3s, constats=3/3, prefill=13.7s, tokens_prompt=43322, messages_max=7)
- ✅ `T4_recherche_multi_sauts` #3 — valeur=True page=True (approbations=2, tool_calls_observés=3, durée=32.9s, constats=3/4, prefill=13.4s, tokens_prompt=52060, messages_max=10)
- ✅ `T5_telechargement_calcul` #1 — masse salariale exacte trouvée (approbations=2, tool_calls_observés=3, durée=17.8s, constats=1/2, prefill=4.6s, tokens_prompt=32477, messages_max=5)
- ✅ `T5_telechargement_calcul` #2 — masse salariale exacte trouvée (approbations=2, tool_calls_observés=3, durée=21.5s, constats=3/4, prefill=5.3s, tokens_prompt=43989, messages_max=9)
- ✅ `T5_telechargement_calcul` #3 — masse salariale exacte trouvée (approbations=2, tool_calls_observés=3, durée=17.7s, constats=1/2, prefill=3.0s, tokens_prompt=26857, messages_max=5)
- ✅ `T6_session_authentifiee` #1 — compte exact trouvé (approbations=4, tool_calls_observés=7, durée=24.2s, constats=3/3, prefill=8.1s, tokens_prompt=54336, messages_max=11)
- ✅ `T6_session_authentifiee` #2 — compte exact trouvé (approbations=4, tool_calls_observés=7, durée=26.3s, constats=4/5, prefill=8.4s, tokens_prompt=56054, messages_max=11)
- ✅ `T6_session_authentifiee` #3 — compte exact trouvé (approbations=4, tool_calls_observés=7, durée=28.2s, constats=4/5, prefill=10.1s, tokens_prompt=56342, messages_max=11)
- ✅ `T7_impossible_par_construction` #1 — absence_declaree=True prix_invente=False (approbations=5, tool_calls_observés=15, durée=117.4s, URL fabriquées=['http://fixture-catalog/catalog/page-4.html', 'http://fixture-catalog/catalog/'], constats=6/6, prefill=30.6s, tokens_prompt=293930, messages_max=41)
- ✅ `T7_impossible_par_construction` #2 — absence_declaree=True prix_invente=False (approbations=6, tool_calls_observés=10, durée=116.9s, constats=9/10, prefill=26.5s, tokens_prompt=114076, messages_max=21)
- ✅ `T7_impossible_par_construction` #3 — absence_declaree=True prix_invente=False (approbations=6, tool_calls_observés=11, durée=130.5s, constats=11/12, prefill=44.6s, tokens_prompt=176389, messages_max=25)
- ✅ `T8_wikipedia` #1 — Muret trouvé (approbations=5, tool_calls_observés=11, durée=83.7s, constats=7/7, prefill=38.4s, tokens_prompt=195085, messages_max=27)
- ❌ `T8_wikipedia` #2 — Muret absent de la réponse (approbations=4, tool_calls_observés=9, durée=66.5s, cause=infra, constats=6/6, prefill=32.7s, tokens_prompt=176242, messages_max=27)
- ✅ `T8_wikipedia` #3 — Muret trouvé (approbations=4, tool_calls_observés=10, durée=81.8s, constats=6/6, prefill=30.9s, tokens_prompt=137386, messages_max=23)
- ✅ `T9_google_insee` #1 — insee trouvé (approbations=8, tool_calls_observés=15, durée=135.0s, constats=9/9, prefill=89.4s, tokens_prompt=187834, messages_max=19)
- ✅ `T9_google_insee` #2 — insee trouvé (approbations=8, tool_calls_observés=15, durée=125.3s, constats=9/9, prefill=35.8s, tokens_prompt=110744, messages_max=19)
- ✅ `T9_google_insee` #3 — insee trouvé (approbations=4, tool_calls_observés=9, durée=39.5s, constats=6/6, prefill=11.3s, tokens_prompt=68413, messages_max=13)
- ✅ `T10_books_toscrape` #1 — titre+prix exacts trouvés (approbations=9, tool_calls_observés=18, durée=147.7s, constats=11/11, prefill=67.0s, tokens_prompt=180970, messages_max=27)
- ✅ `T10_books_toscrape` #2 — titre+prix exacts trouvés (approbations=9, tool_calls_observés=19, durée=179.7s, constats=11/11, prefill=87.5s, tokens_prompt=200803, messages_max=27)
- ✅ `T10_books_toscrape` #3 — titre+prix exacts trouvés (approbations=11, tool_calls_observés=22, durée=251.8s, constats=14/14, prefill=126.5s, tokens_prompt=282226, messages_max=37)
- ✅ `T11_sonde_peremption` #1 — version 3.14.7 trouvée (approbations=2, tool_calls_observés=3, durée=19.3s, constats=2/3, prefill=6.9s, tokens_prompt=52455, messages_max=7)
- ✅ `T11_sonde_peremption` #2 — version 3.14.7 trouvée (approbations=2, tool_calls_observés=3, durée=21.4s, constats=2/3, prefill=3.7s, tokens_prompt=26884, messages_max=7)
- ✅ `T11_sonde_peremption` #3 — version 3.14.7 trouvée (approbations=2, tool_calls_observés=3, durée=16.1s, constats=2/2, prefill=9.2s, tokens_prompt=40897, messages_max=5)
