# Instruction particulières pour ce projet

1. lis `CLAUDE.md`, le README (court) et le brief du chantier en cours en
   début de session ; `HISTORY.md`/`RESOLVED_BUGS.md` se consultent par
   recherche ciblée (grep sur un mot-clé), jamais en entier.
2. les bugs résolus doivent être inscrits dans RESOLVED_BUGS.md
3. l'historique des avancées doit être inscrit dans HISTORY.md
4. toujours informer l'utilisateur des commandes qu'il doit tapper si besoin de redémarrer/rebuild un service docker
5. une phase = une PR. Un commit = une nature de changement (jamais un
   déplacement et une réécriture dans le même commit — le diff devient
   illisible et l'historique de fichiers, argument central de ce dépôt,
   se perd).
6. STOP 🧑 aux checkpoints.
7. Pas de refactor opportuniste hors périmètre — le proposer au checkpoint.
8. Toute affirmation sur le comportement d'une lib se vérifie contre le code installé.
9. README mis à jour au fil de l'eau, style existant.
10. Suggérer des simplification évidentes quand c'est opportun
11. Langue du code/doc (chantier « restructuration + anglais », voir
    `docs/briefs/`) : nouveau contenu en anglais — docstrings, commentaires,
    identifiants internes non exposés, README/CLAUDE.md/PLAN.md/docs.
    Restent en français : prompts système/directives envoyés au modèle et
    prompts des tâches du benchmark (comportement, pas documentation —
    traduction isolée en phase 6, jamais mêlée à un refactor) ; les entrées
    déjà écrites de `HISTORY.md`/`RESOLVED_BUGS.md` (archives datées) ;
    messages d'approbation/notices utilisateur (décision séparée à venir).

## Contrat docstrings/commentaires

- une ligne par défaut ; docstring développée seulement si le comportement
  est non évident (effet de bord, invariant, contrat d'erreur, POURQUOI de
  ce choix) ;
- jamais de paraphrase de la signature, jamais de section
  Args/Returns/Raises quand noms et types suffisent déjà ;
- le commentaire explique le POURQUOI ; un code qui exige d'expliquer le
  QUOI se réécrit plutôt ;
- l'historique ne vit pas dans le code (« corrigé à l'itération 3 ») → renvoi
  d'une ligne vers `HISTORY.md`/`RESOLVED_BUGS.md`, jamais le détail recopié ;
- **exception assumée** : un bloc qui documente une contrainte externe
  vérifiée (comportement d'une lib, piège d'un backend, raison d'un flag)
  reste — c'est du savoir chèrement acquis. On coupe la paraphrase, on
  garde la justification.

## Contrat markdown

Pas de résumé de ce qui précède, pas de section « Conclusion »/« Points
clés » dans la doc technique, pas de tableau quand trois lignes suffisent.
Un document = une fonction.


# Contexte

La stack sert désormais Qwen3.6-27B EXL3 via
TabbyAPI/ExLlamaV3 (dual-GPU, vision + MTP), le trio langgraph/langchain-openai/
openai est migré en 1.x/2.x, et un serveur MCP Playwright est branché aux côtés
de GhostDesk. Objectif du chantier : faire passer l'agent de « exécute des
actions approuvées » à « accomplit des tâches web multi-étapes en autonomie »,
sans affaiblir le modèle de sécurité existant (tiers d'approbation, PromptGuard,
firewall egress).


# Plan de développement

Voir `PLAN.md` — plan détaillé par phases (0 à 4), amendements intégrés.