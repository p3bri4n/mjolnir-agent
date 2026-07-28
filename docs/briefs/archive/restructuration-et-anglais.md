# Chantier « Restructuration + passage à l'anglais » — brief d'exécution

> **Prérequis** : le lot en cours (persistance des campagnes, flags du cœur
> cognitif, angle mort d'audit, mode bulk de `browser_extract`) est livré et le
> checkpoint complet 33 runs est passé. Exception : les phases 1 et 3
> (allègement + découpage du README) peuvent être avancées, leur bénéfice est
> immédiat sur chaque session.
>
> **Objectif** : dé-« PoC »-iser le dépôt — alléger, réorganiser, passer en
> anglais — SANS changer une seule ligne de comportement mesuré. Ce chantier
> se termine par une campagne dont le seul but est de prouver qu'il n'a rien
> changé.
>
> **Règle structurante — un commit = une nature de changement.** Jamais de
> déplacement et de réécriture dans le même commit : sinon le diff devient
> illisible et l'historique de fichiers (l'argument principal de ce dépôt)
> est perdu. Ordre imposé : contrat → allègement → déplacements → découpage →
> traduction.
>
> **Interdits transverses** : aucune modification des prompts système et
> directives (phase 6, chantier isolé) ; aucune modification des prompts de
> tâches ni des fixtures du benchmark (gel) ; aucun refactor de logique ;
> aucune correction de bug « au passage » (la noter, la proposer au
> checkpoint). Les tests sont le juge de neutralité à chaque phase : ils
> doivent passer sans être modifiés, sauf ajustement de chemin d'import.

---

## Phase 0 — Contrats dans CLAUDE.md (avant tout code)

1. Ajouter le **contrat docstrings/commentaires** :
   - une ligne par défaut ; docstring développée uniquement si le comportement
     est non évident (effets de bord, invariants, contrat d'erreur, POURQUOI
     ce choix) ;
   - jamais de paraphrase de la signature, jamais de sections
     Args/Returns/Raises quand noms et types suffisent ;
   - le commentaire explique le POURQUOI ; un code qui exige qu'on explique
     le QUOI se réécrit ;
   - l'historique ne vit pas dans le code (« corrigé à l'itération 3 ») →
     `history.md` / `bugs-resolus.md`, au plus une ligne de renvoi ;
   - **exception assumée** : les blocs qui documentent une contrainte externe
     vérifiée (comportement d'une lib, piège d'un backend, raison d'un flag)
     RESTENT — c'est du savoir chèrement acquis. Consigne : couper la
     paraphrase, garder la justification.
2. Ajouter le **contrat markdown** : pas de résumé de ce qui précède, pas de
   section « Conclusion »/« Points clés » dans la doc technique, pas de
   tableau quand trois lignes suffisent. Un document = une fonction.
3. Remplacer la règle #1 (« lis le README.md en début de session ») par :
   « lis `CLAUDE.md`, le README (court) et le brief du chantier en cours ;
   `history.md` et `bugs-resolus.md` se consultent par recherche ciblée,
   jamais en entier. »
4. Ajouter la règle de langue (voir phase 4) et la règle « un commit = une
   nature de changement ».
🧑 Checkpoint court : revue des contrats.

## Phase 1 — Allègement (en français, diff soustractif)

Passe sur `services/*/app/*.py` et les tests, contrat de phase 0 appliqué.

- **Diff purement soustractif** : on coupe, on ne réécrit pas. Toute
  reformulation qui change le sens est hors périmètre.
- **Zéro modification de code exécutable.** Vérification : les tests passent
  sans être touchés, et `git diff --stat` ne doit toucher que des lignes de
  commentaire/docstring.
- Signaler au checkpoint (sans corriger) : docstrings qui décrivaient un
  comportement FAUX — ce sont des bugs de documentation, potentiellement des
  indices de bugs réels.
- Cible indicative : `graph.py` (2820 lignes) est le principal gisement.
  Rendre compte du volume retiré par fichier.
🧑 Checkpoint : diff relu avant commit.

## Phase 2 — Déplacements et renommages (`git mv` seul, contenu intact)

Arborescence cible :

```
README.md            (raccourci en phase 3)
CLAUDE.md
PLAN.md
docker-compose.yml
.env.example
docs/
  architecture/{autonomy,tool-supervision,inference-backend,observability}.md
  operations/{testing,campaigns,runbook}.md
  briefs/
  campaigns/          # rapports + .DONE + futurs campaign-*.json
  history.md
  resolved-bugs.md
  benchmark-v1.md
  assets/logo.png
```

Renommages nommément :

| Actuel | Cible | Raison |
|---|---|---|
| `docs/history.md` | `docs/history.md` | dégage la racine |
| `docs/resolved-bugs.md` | `docs/resolved-bugs.md` | idem |
| `docs/benchmark-v1.md` | `docs/benchmark-v1.md` | « 0 » = point zéro, pas version ; collision avec la v2 prévue |
| `CAMPAIGN_DURATION_STATS.json` | `_duration_estimates.cache.json` | c'est un cache glissant, pas un historique |
| `logo-agentic-ai-playground.jpg` | `docs/assets/logo.png` | nom neutre, survit au renommage du projet |
| `TASKS-*.md` / `.DONE` | `docs/campaigns/<date>_<type>_<label>.md` | voir convention ci-dessous |

Convention de rapports : `AAAA-MM-JJ_type_label` avec
`type ∈ {baseline, campaign, smoke, diagnostic}` — tri chronologique naturel,
type lisible, label thématique (pas de numéro d'itération). Le `.md`, le
`.DONE` et le futur `.json` partagent le même radical.

Mettre à jour les chemins référencés (harnais, scripts, `run-campaign.sh`,
liens markdown) dans un commit SÉPARÉ juste après, puis vérifier : tests
verts, `run-campaign.sh --help` et un smoke 1 tâche fonctionnels.
🧑 Checkpoint.

## Phase 3 — Découpage du README

1. Le README devient une porte d'entrée : ce qu'est le projet, démarrage
   rapide, carte de l'architecture (arborescence + rôle de chaque service en
   une ligne), limites assumées, liens vers `docs/`. **Cible ≤ 250 lignes.**
2. Sections déplacées telles quelles (pas de réécriture à ce stade) :
   « Autonomie » → `docs/architecture/autonomy.md` ; « Supervision humaine »
   → `docs/architecture/tool-supervision.md` ; « Backend d'inférence » →
   `docs/architecture/inference-backend.md` ; « Observabilité » +
   « Persistance » → `docs/architecture/observability.md` ; « Tests » +
   « Streaming SSE » → `docs/operations/testing.md` ; commandes de
   rebuild/redémarrage → `docs/operations/runbook.md`.
3. `PLAN.md` : extraire l'état d'avancement dans
   `docs/project-status.md` (change à chaque checkpoint) ; `PLAN.md` garde la
   feuille de route (change rarement) et reste la source de vérité.
4. `docs/resolved-bugs.md` : convertir le tableau géant (cellules de ~2000
   caractères, indiffables) en une section par bug — `### Titre` puis
   symptôme / cause confirmée / correctif revérifié. Contenu identique,
   aucune perte.
5. **Critère de sortie mesurable** : le lot chargé en début de session
   (`CLAUDE.md` + README + brief courant) tient sous 5 000 tokens. Le mesurer
   et le consigner.
🧑 Checkpoint.

## Phase 4 — Traduction (docs + code non comportemental)

**Périmètre traduit** : docstrings et commentaires, noms internes non
exposés (variables, fonctions privées, messages de log), README, `CLAUDE.md`,
`PLAN.md`, `docs/architecture/*`, `docs/operations/*`, `docs/briefs/*`,
`docs/benchmark-v1.md`.

**Périmètre NON traduit, à laisser strictement intact** :
- prompts système et directives (`GROUNDING_DIRECTIVE`, `DOWNLOAD_DIRECTIVE`,
  `BULK_CHECK_DIRECTIVE`, `PEREMPTION_DIRECTIVE`, `PLANNER_SYSTEM_PROMPT`,
  `PLAN_JUDGE_SYSTEM_PROMPT`, et toute chaîne envoyée au modèle) → phase 6 ;
- prompts des tâches T1-T11, assertions et fixtures du benchmark → gel ;
- `docs/history.md` et `docs/resolved-bugs.md` → archives datées, restent en
  français ; les nouvelles entrées s'écrivent en anglais à partir de ce
  chantier (règle à inscrire dans `CLAUDE.md`) ;
- messages d'approbation et notices destinés à l'utilisateur → laissés en
  français dans cette phase, décision séparée au checkpoint.

Ne PAS renommer les clés d'environnement ni les noms de services : elles
vivent dans `.env`, les volumes, les URLs internes et les scripts — gain
cosmétique, coût de rupture réel.
🧑 Checkpoint.

## Phase 5 — Preuve de neutralité

Campagne complète 33 runs via `run-campaign.sh`, préambule vert.
**Critère unique** : résultats statistiquement indistinguables du checkpoint
précédent (score, latence médiane, couverture, prefill). Ce n'est pas une
campagne d'amélioration — c'est la preuve que 5 phases de remaniement n'ont
rien cassé. Tout écart notable = investigation avant clôture.
🧑 Checkpoint final du chantier.

## Phase 6 — Traduction des prompts (chantier ISOLÉ, après clôture)

À ne PAS inclure ici. Les directives et prompts système sont du comportement,
pas de la documentation : les traduire est un changement à une variable, qui
peut améliorer ou dégrader les résultats (les modèles multilingues suivent
souvent mieux l'anglais — hypothèse à mesurer, pas à supposer). Protocole :
traduction seule, aucune autre modification, smoke puis campagne complète,
juges habituels (score, latence, couverture). Revert si dégradation.

## Après : baptême Mjolnir

Commit dédié une fois la phase 5 verte : dépôt en `mjolnir-agent`, `name:
mjolnir` dans le compose, README et logo. **Les noms de services ne changent
pas** (`langgraph-agent`, `mcp-client`, `tabbyapi`, `ocr-service`…) : ils
décrivent des fonctions, pas une marque.
