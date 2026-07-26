FLAGS DU CŒUR COGNITIF — trois correctifs, à faire AVANT le checkpoint
complet (avec le chantier persistance, même lot) :

1. DÉFAUTS INVERSÉS : PLANNER_ENABLED, VERIFICATION_ENABLED,
   PLAN_VALIDATION_ENABLED, PLAN_JUDGE_ENABLED passent à "true" par
   défaut dans app/graph.py (os.environ.get(..., "true")) et dans
   .env.example. Justification à consigner dans les commentaires
   existants : le défaut "false" datait de la validation itération par
   itération ; le cœur cognitif est mesuré et adopté, le comportement
   nominal doit être le défaut, c'est la DÉSACTIVATION qui doit être
   explicite. Vérifier que les tests unitaires qui supposaient le défaut
   "false" sont ajustés (ils doivent forcer explicitement la valeur
   qu'ils testent, pas dépendre du défaut).

2. PRÉAMBULE DE CAMPAGNE — contrôle des flags effectifs
   (campaign_preflight.py, à côté de check_tools_schema et
   check_tabbyapi_image_fresh) : nouvelle vérification
   check_agent_flags() qui lit les variables effectives DANS le
   conteneur (docker exec langgraph-agent env) et les compare à un
   attendu déclaré dans le harnais. Écart → campagne refusée avec le
   diff. Couvre les 4 flags ci-dessus + les autres variables qui
   pilotent le comportement mesuré (MAX_TOOL_ITERATIONS, budgets de
   tentatives/replanification, thinking bridé, overrides de tiers,
   seuils de tronquage) — dresser la liste exacte depuis graph.py, ne
   pas la deviner.

3. SÉRIALISATION : ces mêmes flags effectifs vont dans les métadonnées
   du campaign-<id>.json (point 1 du brief persistance) — une campagne
   doit dire quel agent elle a mesuré, pas seulement ce qu'il a fait.

Note pour la doc (README, section exploitation) : ces flags sont lus au
niveau module, donc tout changement exige
`docker compose up -d --force-recreate langgraph-agent` — un restart ne
suffit pas. Les définir dans le shell n'a AUCUN effet (le harnais parle
à l'agent en HTTP ; les flags vivent dans le process serveur) : c'est un
piège silencieux, à documenter comme tel.