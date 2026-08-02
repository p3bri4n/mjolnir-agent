# family-e-mesure (docs/briefs/B3-benchmark-v2.md)

Générée automatiquement le 2026-07-30T16:56:48.344552+00:00.

## Famille E — canaux de perception

E1 : le code n'est lisible que dans l'arbre d'accessibilité (DOM hors-écran, absent de toute capture). E2 : le code n'est lisible que par capture visuelle (texte dessiné en pixels dans un `<canvas>`, absent du DOM). Pour ces deux tâches, seul le succès/échec de l'extraction est jugé — la réussite prouve à elle seule que le canal fonctionne quand il est la seule voie possible. E3 : le code est lisible par les deux canaux indifféremment — le juge n'est jamais la correction (déjà garantie) mais si une capture d'écran est jamais entrée dans le contexte (`/context`, bloc « images », le seul moyen de l'observer — le journal d'audit ne journalise jamais les outils TIER_READ comme browser_snapshot/browser_extract/browser_take_screenshot, voir docstring du module).

- **E1_dom_only** : 3/3
- **E2_visual_only** : 1/3
- **E3_routing_equivalence** : 3/3 (capture visuelle utilisée : 0/3)

## Détail par run

- ✅ `E1_dom_only` #1 — code trouvé (durée=9.8s)
- ✅ `E1_dom_only` #2 — code trouvé (durée=9.8s)
- ✅ `E1_dom_only` #3 — code trouvé (durée=16.4s)
- ❌ `E2_visual_only` #1 — code ZK-3392 absent de la réponse (durée=41.0s, cause=extraction)
- ❌ `E2_visual_only` #2 — code ZK-3392 absent de la réponse (durée=141.5s, cause=extraction)
- ✅ `E2_visual_only` #3 — code trouvé (durée=34.0s)
- ✅ `E3_routing_equivalence` #1 — code trouvé (durée=10.3s)
- ✅ `E3_routing_equivalence` #2 — code trouvé (durée=15.7s)
- ✅ `E3_routing_equivalence` #3 — code trouvé (durée=11.1s)
