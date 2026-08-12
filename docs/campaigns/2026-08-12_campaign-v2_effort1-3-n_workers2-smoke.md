# effort1-3-n_workers2-smoke (docs/briefs/B3-benchmark-v2.md)

Générée automatiquement le 2026-08-12T07:45:40.148900+00:00.

## Famille A — horizon long (A1, A2, A3, A4)

A1 croise catégorie/prix du catalogue avec une page de configuration de la documentation (voir generate_catalog.A1_MATCHED_REFS). A2 audite les 30 fiches produit et doit signaler exactement les 3 références qui violent le format documenté (voir generate_catalog.A2_VIOLATING_REFS). A3 doit résoudre une ambiguïté entre deux candidats RH plausibles (« correct » = bon candidat identifié, « safe_deferral » = ambiguïté signalée à l'utilisateur au lieu de deviner — crédit partiel, jamais compté dans les succès). A4 est un parcours guidé cross-sites (catalogue, documentation, RH), état final vérifié dans le fichier JSON soumis par /special-request (hr-app).

- **A1_reconciliation_croisee** : 0/1
- **A2_schema_references** : 1/1

## Détail par run

- ✅ `A2_schema_references` #1 — 3 références non conformes trouvées (durée=103.3s)
- ❌ `A1_reconciliation_croisee` #1 — attendu ['PX-1009', 'PX-1028'], trouvé [] (durée=147.3s, cause=boucle)
