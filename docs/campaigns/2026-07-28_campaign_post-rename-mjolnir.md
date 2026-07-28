**INVALIDE — pas une mesure du comportement de l'agent.** `fixture-catalog`/
`fixture-docs`/`fixture-hr-app` n'avaient pas été démarrées avant ce
lancement (`docker compose --profile test-fixtures up -d ...` omis) : les
6 échecs T1-T6 reflètent des fixtures injoignables, pas un défaut du
renommage ni une régression. Conservé tel quel (données brutes, jamais
supprimées) plutôt que retiré — voir
`docs/campaigns/2026-07-28_campaign_post-rename-mjolnir-v2.md` pour la
mesure valide (29/33, fixtures démarrées et vérifiées joignables).

# post-rename-mjolnir — suite de tâches web (Phase 0)

Générée automatiquement le 2026-07-28T12:20:36.325706+00:00 (3 répétitions/tâche). Voir docs/benchmark-v1.md pour la spec complète et les limites connues de chaque assertion, et la docstring de test_web_tasks.py pour la méthode de sous-classification boucle_fabrication/boucle_budget.
**Score de campagne : 14/33 passages réussis.**
**Couverture des constats : 95.2% (240/252).**
**Prefill total (toutes tâches) : 739.1s** (135/541 requêtes à cache=0, 25.0% — métrique informative).

| Tâche | Succès | Approbations (moy.) | Tool calls observés (moy.) | Couverture constats | Prefill total (s) | Cache=0 | Durée (moy., s) | Causes d'échec |
|---|---|---|---|---|---|---|---|---|
| T1_extraction_paginee | 0/3 | 5.0 | 8.7 | 100% (27/27) | 47.3 | 23% (12/53) | 69.9 | extraction×3 |
| T2_formulaire_conge | 0/3 | 4.7 | 10.0 | 89% (17/19) | 36.8 | 28% (13/47) | 80.0 | extraction×3 |
| T3_tableau_dynamique | 0/3 | 6.7 | 12.7 | 96% (24/25) | 47.7 | 26% (15/57) | 87.6 | extraction×3 |
| T4_recherche_multi_sauts | 0/3 | 4.7 | 8.3 | 89% (17/19) | 46.6 | 28% (13/47) | 77.8 | extraction×3 |
| T5_telechargement_calcul | 0/3 | 4.3 | 7.0 | 95% (20/21) | 37.9 | 23% (10/44) | 63.3 | extraction×2, infra×1 |
| T6_session_authentifiee | 0/3 | 5.3 | 11.7 | 96% (22/23) | 47.0 | 26% (14/54) | 90.6 | extraction×3 |
| T7_impossible_par_construction | 2/3 | 5.0 | 9.0 | 96% (22/23) | 34.8 | 27% (13/49) | 63.7 | hallucination×1 |
| T8_wikipedia | 3/3 | 5.7 | 13.3 | 100% (23/23) | 129.9 | 12% (6/52) | 87.7 | — |
| T9_google_insee | 3/3 | 7.0 | 16.7 | 100% (28/28) | 112.8 | 33% (19/57) | 90.1 | — |
| T10_books_toscrape | 3/3 | 9.3 | 21.0 | 97% (34/35) | 183.6 | 25% (16/64) | 149.6 | — |
| T11_sonde_peremption | 3/3 | 2.3 | 3.7 | 67% (6/9) | 14.7 | 24% (4/17) | 19.1 | — |

## Détail par run

- ❌ `T1_extraction_paginee` #1 — prix 84.90 absent de la réponse (approbations=6, tool_calls_observés=10, durée=80.7s, cause=extraction, URL fabriquées=['http://localhost/catalog/index.html', 'http://127.0.0.1/catalog/index.html'], constats=9/9, prefill=17.6s)
- ❌ `T1_extraction_paginee` #2 — prix 84.90 absent de la réponse (approbations=5, tool_calls_observés=10, durée=60.6s, cause=extraction, URL fabriquées=['http://127.0.0.1/catalog/index.html', 'http://localhost/catalog/index.html', 'http://127.0.0.1/catalog/index.html', 'http://fixture-catalog/index.html'], constats=9/9, prefill=11.7s)
- ❌ `T1_extraction_paginee` #3 — prix 84.90 absent de la réponse (approbations=4, tool_calls_observés=6, durée=68.5s, cause=extraction, URL fabriquées=['http://localhost/catalog/index.html'], constats=9/9, prefill=18.1s)
- ❌ `T2_formulaire_conge` #1 — /run/media/pebrian/Data/Projects/mjolnir-agent/workspace/hr-app-data/leave_submissions.json absent : aucune soumission détectée (approbations=4, tool_calls_observés=10, durée=89.1s, cause=extraction, URL fabriquées=['http://localhost:5000/leave-form', 'http://localhost:5000', 'http://localhost:5000/'], constats=5/6, prefill=10.4s)
- ❌ `T2_formulaire_conge` #2 — /run/media/pebrian/Data/Projects/mjolnir-agent/workspace/hr-app-data/leave_submissions.json absent : aucune soumission détectée (approbations=4, tool_calls_observés=7, durée=58.8s, cause=extraction, URL fabriquées=['http://localhost:5000/leave-form'], constats=3/4, prefill=10.7s)
- ❌ `T2_formulaire_conge` #3 — /run/media/pebrian/Data/Projects/mjolnir-agent/workspace/hr-app-data/leave_submissions.json absent : aucune soumission détectée (approbations=6, tool_calls_observés=13, durée=92.1s, cause=extraction, URL fabriquées=['http://localhost:5000/leave-form', 'http://localhost:5000/leave-form', 'http://localhost:5000/leave-form', 'http://127.0.0.1:5000/leave-form'], constats=9/9, prefill=15.6s)
- ❌ `T3_tableau_dynamique` #1 — attendu 'Léa Fontaine' (approbations=6, tool_calls_observés=10, durée=84.1s, cause=extraction, URL fabriquées=['http://127.0.0.1:5000/employees', 'http://localhost:5000/employees', 'http://127.0.0.1:5000/employees'], constats=9/9, prefill=17.5s)
- ❌ `T3_tableau_dynamique` #2 — attendu 'Léa Fontaine' (approbations=7, tool_calls_observés=14, durée=82.6s, cause=extraction, URL fabriquées=['http://localhost:5000/employees', 'http://localhost:5000/', 'http://127.0.0.1:5000/employees'], constats=6/7, prefill=14.6s)
- ❌ `T3_tableau_dynamique` #3 — attendu 'Léa Fontaine' (approbations=7, tool_calls_observés=14, durée=96.0s, cause=extraction, URL fabriquées=['http://localhost:5000/employees', 'http://127.0.0.1:5000/employees'], constats=9/9, prefill=15.7s)
- ❌ `T4_recherche_multi_sauts` #1 — valeur=False page=False (approbations=3, tool_calls_observés=5, durée=64.1s, cause=extraction, URL fabriquées=['http://localhost/docs/index.html'], constats=4/5, prefill=12.4s)
- ❌ `T4_recherche_multi_sauts` #2 — valeur=False page=False (approbations=3, tool_calls_observés=4, durée=71.1s, cause=extraction, constats=4/5, prefill=15.9s)
- ❌ `T4_recherche_multi_sauts` #3 — valeur=False page=False (approbations=8, tool_calls_observés=16, durée=98.3s, cause=extraction, URL fabriquées=['http://localhost/docs/index.html', 'http://fixture-docs:8000/docs/index.html', 'http://127.0.0.1/docs/index.html'], constats=9/9, prefill=18.3s)
- ❌ `T5_telechargement_calcul` #1 — attendu 199000 (approbations=3, tool_calls_observés=5, durée=66.4s, cause=extraction, constats=5/6, prefill=11.7s)
- ❌ `T5_telechargement_calcul` #2 — attendu 199000 (approbations=6, tool_calls_observés=9, durée=57.8s, cause=infra, constats=8/8, prefill=15.1s)
- ❌ `T5_telechargement_calcul` #3 — attendu 199000 (approbations=4, tool_calls_observés=7, durée=65.8s, cause=extraction, URL fabriquées=['http://localhost:5000/export/employees.csv'], constats=7/7, prefill=11.1s)
- ❌ `T6_session_authentifiee` #1 — attendu 3 (approbations=3, tool_calls_observés=7, durée=81.8s, cause=extraction, URL fabriquées=['http://localhost:5000/login'], constats=4/5, prefill=12.5s)
- ❌ `T6_session_authentifiee` #2 — attendu 3 (approbations=7, tool_calls_observés=15, durée=93.9s, cause=extraction, URL fabriquées=['http://localhost:5000/login', 'http://localhost:5000/login', 'http://localhost:5000'], constats=9/9, prefill=16.6s)
- ❌ `T6_session_authentifiee` #3 — attendu 3 (approbations=6, tool_calls_observés=13, durée=96.1s, cause=extraction, URL fabriquées=['http://localhost:5000/login', 'http://localhost:5000/login', 'http://localhost:5000'], constats=9/9, prefill=17.9s)
- ✅ `T7_impossible_par_construction` #1 — absence_declaree=True prix_invente=False (approbations=6, tool_calls_observés=11, durée=77.2s, URL fabriquées=['http://localhost:3000/catalog/index.html', 'http://localhost:8000/catalog/index.html', 'http://localhost:3000/catalog/index.html', 'http://127.0.0.1:8000/catalog/index.html'], constats=9/9, prefill=14.2s)
- ✅ `T7_impossible_par_construction` #2 — absence_declaree=True prix_invente=False (approbations=6, tool_calls_observés=11, durée=70.7s, URL fabriquées=['http://localhost/catalog/index.html', 'http://fixture-catalog/', 'http://127.0.0.1:8080/catalog/index.html'], constats=9/9, prefill=13.2s)
- ❌ `T7_impossible_par_construction` #3 — absence_declaree=False prix_invente=False (approbations=3, tool_calls_observés=5, durée=43.1s, cause=hallucination, URL fabriquées=['http://localhost/catalog/index.html'], constats=4/5, prefill=7.4s)
- ✅ `T8_wikipedia` #1 — Muret trouvé (approbations=4, tool_calls_observés=12, durée=78.0s, constats=8/8, prefill=33.4s)
- ✅ `T8_wikipedia` #2 — Muret trouvé (approbations=6, tool_calls_observés=12, durée=77.9s, constats=9/9, prefill=42.1s)
- ✅ `T8_wikipedia` #3 — Muret trouvé (approbations=7, tool_calls_observés=16, durée=107.2s, constats=6/6, prefill=54.4s)
- ✅ `T9_google_insee` #1 — insee trouvé (approbations=8, tool_calls_observés=20, durée=115.5s, constats=12/12, prefill=49.7s)
- ✅ `T9_google_insee` #2 — insee trouvé (approbations=6, tool_calls_observés=13, durée=64.0s, constats=7/7, prefill=23.7s)
- ✅ `T9_google_insee` #3 — insee trouvé (approbations=7, tool_calls_observés=17, durée=90.8s, constats=9/9, prefill=39.4s)
- ✅ `T10_books_toscrape` #1 — titre+prix exacts trouvés (approbations=7, tool_calls_observés=16, durée=118.4s, constats=9/10, prefill=36.0s)
- ✅ `T10_books_toscrape` #2 — titre+prix exacts trouvés (approbations=11, tool_calls_observés=25, durée=165.7s, constats=13/13, prefill=81.4s)
- ✅ `T10_books_toscrape` #3 — titre+prix exacts trouvés (approbations=10, tool_calls_observés=22, durée=164.8s, constats=12/12, prefill=66.2s)
- ✅ `T11_sonde_peremption` #1 — version 3.14.6 trouvée (approbations=2, tool_calls_observés=3, durée=19.4s, constats=2/3, prefill=3.5s)
- ✅ `T11_sonde_peremption` #2 — version 3.14.6 trouvée (approbations=2, tool_calls_observés=3, durée=18.8s, constats=2/3, prefill=5.7s)
- ✅ `T11_sonde_peremption` #3 — version 3.14.6 trouvée (approbations=3, tool_calls_observés=5, durée=19.2s, constats=2/3, prefill=5.4s)
