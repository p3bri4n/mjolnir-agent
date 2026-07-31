> **Status: closed (2026-07-31).** All 6 points delivered — see
> `docs/history.md` (search "BENCHMARK V2", "A4 / COMPACTION", "SONDE DE
> FAISABILITÉ CANAL VISUEL") and `docs/benchmark-v2.md` for full detail.
> Summary result per point:
>
> 1. **B-β hard**: hypothesis falsified by archives alone (zero runs) —
>    BULK_CHECK_DIRECTIVE never referenced in the model's reasoning at
>    the `browser_evaluate` bascule points. Planned conditional correctif
>    abandoned before any code. Real root cause found instead: a `ref=`
>    prefix format defect in `mcp-client`'s target handling (28/28
>    failures measured across every fixture since 2026-07-22) — fixed
>    generically (`_normalize_ref_targets`), plus a new TIER_READ
>    `browser_inspect` tool. **Deviation**: the point's own planned
>    correctif was dropped; a different, better-supported fix shipped
>    instead, checkpointed with the user before any code.
> 2. **CuP semantics**: documented in `docs/benchmark-v2.md` — the
>    harness auto-approves even NEVER_GRANTABLE tools, so CuP measures
>    intention, not deployed safety.
> 3. **Family A4 compaction coverage**: archives (101 threads, only 4
>    reach the threshold, all A4) showed the flag was never even turned
>    on for any A4 run — a flattering zero. **Deviation, agreed at
>    checkpoint**: the point asked for an archives-only coverage
>    diagnosis; it grew into a full targeted live exercise
>    (`probe_compaction_multi_turn.py`, multi-turn threads instead of a
>    single long task after `MAX_TOOL_ITERATIONS` was found to cap a
>    single task at ~41 messages) — measured live, 3 reps × 2 conditions.
>    **Result: a negative one**, not a non-result — compaction ON nearly
>    doubles tool_calls/tokens and drops dependent-turn success from 4/6
>    to 0/6, with real coverage this time (19-26 compactions/run).
>    `EPISODE_COMPACTION_ENABLED` stays `false`. A real `/approve` bug
>    (owui_message_count desync) was found and fixed along the way
>    (`docs/resolved-bugs.md` #44) — the first client to ever chain
>    multiple top-level turns on one thread.
> 4. **Family C 9/9**: documented as a zero point with no progression
>    margin left; scoped to v2.1 (indirect injections, multi-step,
>    canary task), fixtures stay frozen, nothing built.
> 5. **Visual channel feasibility probe**: delivered
>    (`docs/architecture/visual-channel-feasibility.md`) — 8 cases, 3
>    real channels, no agent loop. Conclusion: GhostDesk removal would
>    lose nothing tested; its only unique capability is out-of-browser
>    interaction, already out of scope (E4).
> 6. **CLAUDE.md smoke rule**: added retroactively at closing, having
>    been missed during the session itself (**deviation**, caught only
>    when re-reading this brief to write this status header) — see
>    CLAUDE.md's measurement rules, "a live smoke precedes any final
>    measurement."
>
> Both planned checkpoints (STOP after point 1, STOP after point 5) were
> honored; additional ad-hoc checkpoints were taken around point 3's
> scope growth and the `/approve` fix, per this project's discipline of
> flagging deviations before acting on them, not after.

BENCHMARK V2 — SUITES POST-MESURE. Cinq points, dans cet ordre. Archives
d'abord partout où c'est possible ; aucun correctif sans son juge.

1. B-β HARD — hypothèse à vérifier avant tout correctif (archives, zéro run) :
   BULK_CHECK_DIRECTIVE était-elle présente dans le contexte des runs où
   browser_evaluate a été employé pour contourner l'UI ? Le raisonnement du
   modèle y fait-il référence ? Si oui, la brèche est induite par une
   directive que nous avons écrite, pas par une dérive spontanée.
   Correctif conditionnel : mode bulk dans browser_extract (tier LECTURE,
   template JS fixe, argument déclaratif — jamais de code), puis
   BULK_CHECK_DIRECTIVE réécrite pour pointer vers l'outil au lieu
   d'enseigner browser_evaluate. Juge : CuP de B-β hard, plus non-régression
   sur A1/A2 et sur les tâches d'extraction. Une variable.

2. DOCUMENTER CE QUE MESURE LE CuP (docs/benchmark-v2.md, une note courte) :
   en campagne le harnais auto-approuve, y compris les outils
   NEVER_GRANTABLE. Le CuP mesure donc l'INTENTION de l'agent, pas la sûreté
   du système déployé — en usage réel une approbation humaine serait
   intervenue. Sans cette note, CuP 1/3 sera relu comme « le système laisse
   passer ».

3. FAMILLE A4 — couverture de compaction (archives) : combien de runs ont
   franchi EPISODE_COMPACTION_TURN_THRESHOLD, et combien de compactions
   appliquées par run ? A4 avait pour raison d'être une couverture de 100 %.
   Si elle n'y est pas, le dire et proposer l'ajustement de fixture
   nécessaire — sinon la compaction reste non validée et le non-résultat de
   la campagne du 2026-07-28 n'est pas levé.

4. FAMILLE C — 9/9 à la ligne de base : constater dans docs/benchmark-v2.md
   que la famille ne peut plus servir de point zéro au plan sécurité (aucune
   marge de progression mesurable pour le proxy, le périmètre et le suivi de
   provenance). NE PAS durcir les tâches maintenant (gel des fixtures) :
   consigner comme périmètre v2.1 — injections indirectes, multi-étapes, et
   la tâche canari (jeton unique planté, échec si le jeton apparaît dans une
   requête sortante).

5. SONDE DE FAISABILITÉ CANAL VISUEL (préalable au retrait de GhostDesk,
   session technique, quasi sans appel agent) : matrice canvas 2D avec
   texte, WebGL, image porteuse de texte, PDF dans le visualiseur, iframe
   cross-origin, shadow DOM, SVG à texte (attendu : DOM, aucune capture
   nécessaire), contenu hors viewport. Pour chaque cas : fixture minimale,
   locator.screenshot() sur l'élément → OCR → comparaison à la vérité
   terrain. Livrable : matrice dans docs/architecture/, et la liste précise
   de ce que le retrait ferait perdre. E2 à 1/3 étant un artefact d'outillage
   (confusion entre canaux de capture) et non un constat de capacité, cette
   sonde — pas E2 — est ce qui conditionne le retrait.

6. CLAUDE.md, section mesure — ajouter la règle que ce chantier a validée
   empiriquement : « Un smoke live précède toute mesure finale d'une famille
   ou d'un mécanisme. » Cinq smokes, cinq bugs attrapés avant que la mesure
   ne compte (image Docker périmée, route racine manquante, deux fuites de
   fixture, juge aveugle au journal d'audit).

STOP 🧑 après le point 1 (verdict d'archives) et après le point 5 (matrice).
