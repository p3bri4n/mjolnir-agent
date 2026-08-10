# effort2.3-criterion-smoke (docs/briefs/B3-benchmark-v2.md)

Générée automatiquement le 2026-08-10T14:02:00.442576+00:00.

## Famille A — horizon long (A1, A2, A3, A4)

A1 croise catégorie/prix du catalogue avec une page de configuration de la documentation (voir generate_catalog.A1_MATCHED_REFS). A2 audite les 30 fiches produit et doit signaler exactement les 3 références qui violent le format documenté (voir generate_catalog.A2_VIOLATING_REFS). A3 doit résoudre une ambiguïté entre deux candidats RH plausibles (« correct » = bon candidat identifié, « safe_deferral » = ambiguïté signalée à l'utilisateur au lieu de deviner — crédit partiel, jamais compté dans les succès). A4 est un parcours guidé cross-sites (catalogue, documentation, RH), état final vérifié dans le fichier JSON soumis par /special-request (hr-app).

- **A1_reconciliation_croisee** : 0/3

## Détail par run

- ❌ `A1_reconciliation_croisee` #1 — attendu ['PX-1009', 'PX-1028'], trouvé [] (durée=118.4s, cause=extraction)
- ❌ `A1_reconciliation_croisee` #2 — attendu ['PX-1009', 'PX-1028'], trouvé [] (durée=143.2s, cause=extraction)
- ❌ `A1_reconciliation_croisee` #3 — attendu ['PX-1009', 'PX-1028'], trouvé [] (durée=112.0s, cause=extraction)
