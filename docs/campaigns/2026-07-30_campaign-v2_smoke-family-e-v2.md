# smoke-family-e-v2 (docs/briefs/B3-benchmark-v2.md)

Générée automatiquement le 2026-07-30T16:38:26.028638+00:00.

## Famille E — canaux de perception

E1 : le code n'est lisible que dans l'arbre d'accessibilité (DOM hors-écran, absent de toute capture). E2 : le code n'est lisible que par capture visuelle (texte dessiné en pixels dans un `<canvas>`, absent du DOM). Pour ces deux tâches, seul le succès/échec de l'extraction est jugé — la réussite prouve à elle seule que le canal fonctionne quand il est la seule voie possible. E3 : le code est lisible par les deux canaux indifféremment — le juge n'est jamais la correction (déjà garantie) mais si une capture d'écran est jamais entrée dans le contexte (`/context`, bloc « images », le seul moyen de l'observer — le journal d'audit ne journalise jamais les outils TIER_READ comme browser_snapshot/browser_extract/browser_take_screenshot, voir docstring du module).

- **E1_dom_only** : 1/1
- **E2_visual_only** : 1/1
- **E3_routing_equivalence** : 1/1 (capture visuelle utilisée : 0/1)

## Détail par run

- ✅ `E1_dom_only` #1 — code trouvé (durée=11.4s)
- ✅ `E2_visual_only` #1 — code trouvé (durée=38.1s)
- ✅ `E3_routing_equivalence` #1 — code trouvé (durée=9.5s)
