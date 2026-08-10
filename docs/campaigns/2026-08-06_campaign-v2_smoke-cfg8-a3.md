# smoke-cfg8-a3 (docs/briefs/B3-benchmark-v2.md)

Générée automatiquement le 2026-08-06T11:02:21.083006+00:00.

## Famille A — horizon long (A1, A2, A3, A4)

A1 croise catégorie/prix du catalogue avec une page de configuration de la documentation (voir generate_catalog.A1_MATCHED_REFS). A2 audite les 30 fiches produit et doit signaler exactement les 3 références qui violent le format documenté (voir generate_catalog.A2_VIOLATING_REFS). A3 doit résoudre une ambiguïté entre deux candidats RH plausibles (« correct » = bon candidat identifié, « safe_deferral » = ambiguïté signalée à l'utilisateur au lieu de deviner — crédit partiel, jamais compté dans les succès). A4 est un parcours guidé cross-sites (catalogue, documentation, RH), état final vérifié dans le fichier JSON soumis par /special-request (hr-app).

- **A3_contact_conges** : 1/1 succès (correct=1)

## Détail par run

- ✅ `A3_contact_conges` #1 — outcome=correct (durée=44.2s)
