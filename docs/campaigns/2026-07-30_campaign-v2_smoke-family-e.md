# smoke-family-e (docs/briefs/B3-benchmark-v2.md)

Générée automatiquement le 2026-07-30T16:13:51.321817+00:00.

## Famille E — canaux de perception

E1 : le code n'est lisible que dans l'arbre d'accessibilité (DOM hors-écran, absent de toute capture). E2 : le code n'est lisible que par capture visuelle (texte dessiné en pixels dans un `<canvas>`, absent du DOM). Pour ces deux tâches, seul le succès/échec de l'extraction est jugé — la réussite prouve à elle seule que le canal fonctionne quand il est la seule voie possible. E3 : le code est lisible par les deux canaux indifféremment — le juge n'est jamais la correction (déjà garantie) mais quel outil (`browser_snapshot` vs `browser_take_screenshot`) a été appelé EN PREMIER, lu depuis le journal d'audit.

- **E1_dom_only** : 1/1
- **E2_visual_only** : 1/1
- **E3_routing_equivalence** : 1/1 (premier canal : none=1)

## Détail par run

- ✅ `E1_dom_only` #1 — code trouvé (durée=10.5s)
- ✅ `E2_visual_only` #1 — code trouvé (durée=11.1s)
- ✅ `E3_routing_equivalence` #1 — code trouvé (durée=14.6s)
