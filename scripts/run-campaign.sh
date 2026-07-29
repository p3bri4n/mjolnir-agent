#!/usr/bin/env bash
# Outillage de campagne (voir docs/history.md, "OUTILLAGE DE CAMPAGNE") : lance le
# harnais de tâches web (services/langgraph-agent/tests_integration/
# test_web_tasks.py) de bout en bout, zéro intervention entre le lancement et
# le rapport. Enchaîne : préambule (readiness LLM réelle + schéma d'outils,
# voir campaign_preflight.py) -> campagne (complète ou smoke) -> rapport
# écrit -> notification de fin.
#
# Usage :
#   scripts/run-campaign.sh                        # campagne complète (11 tâches x 3)
#   scripts/run-campaign.sh --tasks T1,T7,T11       # smoke ciblé (voir SMOKE_TASK_PREFIXES)
#   scripts/run-campaign.sh --tasks T7 --reps 1     # smoke minimal, une seule tâche
#   scripts/run-campaign.sh --label "post-correctif-X"
#   scripts/run-campaign.sh --pause <campaign-id>              # demande de pause (voir Part 2.1)
#   scripts/run-campaign.sh --pause <campaign-id> --release    # + arrêt tabbyapi/playwright-mcp/fixtures une fois la pause confirmée
#   scripts/run-campaign.sh --resume <campaign-id>              # reprise (préambule complet rejoué, refuse sur dérive de config)
#   scripts/run-campaign.sh --suite v2                          # benchmark v2 (docs/briefs/B3-benchmark-v2.md) — famille F seule pour l'instant
#   scripts/run-campaign.sh --suite v2 --resume <campaign-id>   # --suite requis pour reprendre une campagne v2 (le cid seul ne dit pas quelle suite)
#
# Protocole (voir docstring de test_web_tasks.py, WEB_TASKS_SMOKE_TASKS) :
# le mode smoke (--tasks) sert à ITÉRER vite sur un correctif — n réduit,
# pas de signification statistique pour arbitrer un seuil de passage. Seule
# la campagne complète (par défaut, --reps 3 sur les 11 tâches) compte
# comme mesure de référence pour un checkpoint.
#
# Pause/reprise (docs/briefs/B2-campaign-control.md, Part 2) : --pause crée
# un fichier sentinel, lu par le harnais EN COURS D'EXÉCUTION (dans un autre
# terminal) à la prochaine frontière de run — ce script lui-même ne stoppe
# jamais un service tant que --release n'est pas explicitement demandé, et
# --release attend la confirmation `paused=true` avant de le faire (jamais
# pendant qu'un run est en vol).
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"
AGENT_DIR="$PROJECT_DIR/services/langgraph-agent"
CAMPAIGNS_DIR="$PROJECT_DIR/docs/campaigns"

TASKS=""
REPS=""
LABEL=""
REPORT_PATH=""
PAUSE_CID=""
RESUME_CID=""
RELEASE=0
SUITE="v1"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tasks) TASKS="$2"; shift 2 ;;
    --reps) REPS="$2"; shift 2 ;;
    --label) LABEL="$2"; shift 2 ;;
    --report-path) REPORT_PATH="$2"; shift 2 ;;
    --pause) PAUSE_CID="$2"; shift 2 ;;
    --resume) RESUME_CID="$2"; shift 2 ;;
    --release) RELEASE=1; shift ;;
    --suite) SUITE="$2"; shift 2 ;;
    *) echo "Argument inconnu : $1" >&2; exit 1 ;;
  esac
done

if [[ "$SUITE" != "v1" && "$SUITE" != "v2" ]]; then
  echo "--suite doit valoir v1 ou v2 (reçu : $SUITE)" >&2
  exit 1
fi
if [[ "$SUITE" == "v2" && -n "$TASKS" ]]; then
  echo "--tasks n'a pas de sens avec --suite v2 (pas de filtre smoke côté benchmark v2, famille F seule pour l'instant)" >&2
  exit 1
fi
# Défaut de --reps dépendant de la suite (3 pour v1, 2 pour v2 — famille F,
# voir docs/briefs/B3-benchmark-v2.md, "2 repetitions instead of 3").
[[ -z "$REPS" ]] && REPS=$([[ "$SUITE" == "v2" ]] && echo 2 || echo 3)

VENV_PYTHON="${VENV_PYTHON:-$AGENT_DIR/.venv/bin/python}"
if [[ ! -x "$VENV_PYTHON" ]]; then
  VENV_PYTHON="python3"
fi

# ─────────────────────────────────────────────────────────────────────────
# --pause : dépose le sentinel, optionnellement attend la confirmation
# (paused=true dans le progress.json) puis relâche les services — voir Part
# 2.1/2.2 du brief. Un run peut durer plusieurs minutes (famille A) : délai
# large avant d'abandonner l'attente sans rien arrêter.
# ─────────────────────────────────────────────────────────────────────────
if [[ -n "$PAUSE_CID" ]]; then
  PROGRESS_PATH="$CAMPAIGNS_DIR/${PAUSE_CID}.progress.json"
  if [[ ! -f "$PROGRESS_PATH" ]]; then
    echo "Campagne introuvable : $PROGRESS_PATH" >&2
    exit 1
  fi
  touch "$CAMPAIGNS_DIR/${PAUSE_CID}.pause"
  echo "Sentinel de pause créé : $CAMPAIGNS_DIR/${PAUSE_CID}.pause — la campagne s'arrêtera à la fin du run en cours."

  if [[ "$RELEASE" == "1" ]]; then
    echo "Attente de la confirmation de pause (paused=true dans $PROGRESS_PATH)..."
    TIMEOUT_SECONDS=1800
    WAITED=0
    PAUSED="False"
    while [[ "$PAUSED" != "True" ]]; do
      if (( WAITED >= TIMEOUT_SECONDS )); then
        echo "avertissement : pause non confirmée après ${TIMEOUT_SECONDS}s — services NON arrêtés (le run en cours tourne peut-être encore)." >&2
        exit 1
      fi
      sleep 5
      WAITED=$((WAITED + 5))
      PAUSED="$("$VENV_PYTHON" -c "import json,sys; print(json.load(open(sys.argv[1])).get('paused', False))" "$PROGRESS_PATH" 2>/dev/null || echo "False")"
    done
    echo "Campagne en pause confirmée — arrêt de tabbyapi/playwright-mcp/fixtures (jamais fait automatiquement sans --release)."
    ( cd "$PROJECT_DIR" && docker compose stop tabbyapi playwright-mcp fixture-catalog fixture-docs fixture-hr-app )
  fi
  exit 0
fi

# Convention de rapports (Phase 2, restructuration+anglais) :
# AAAA-MM-JJ_type_label.md sous docs/campaigns/ — tri chronologique
# naturel, type lisible (campaign/smoke), label thématique.
if [[ -z "$REPORT_PATH" ]]; then
  if [[ "$SUITE" == "v2" ]]; then
    if [[ -n "$RESUME_CID" ]]; then
      REPORT_PATH="$CAMPAIGNS_DIR/$(date +%Y-%m-%d)_campaign-v2_resume-${RESUME_CID}.md"
    else
      REPORT_PATH="$CAMPAIGNS_DIR/$(date +%Y-%m-%d)_campaign-v2_famille-f.md"
    fi
  elif [[ -n "$RESUME_CID" ]]; then
    REPORT_PATH="$CAMPAIGNS_DIR/$(date +%Y-%m-%d)_campaign_resume-${RESUME_CID}.md"
  elif [[ -n "$LABEL" ]]; then
    REPORT_PATH="$CAMPAIGNS_DIR/$(date +%Y-%m-%d)_campaign_${LABEL}.md"
  elif [[ -n "$TASKS" ]]; then
    REPORT_PATH="$CAMPAIGNS_DIR/$(date +%Y-%m-%d)_smoke_adhoc-$(date +%H%M%S).md"
  else
    REPORT_PATH="$CAMPAIGNS_DIR/$(date +%Y-%m-%d)_campaign_full.md"
  fi
fi

STATS_PATH="$AGENT_DIR/tests_integration/DURATION_ESTIMATE_CACHE.json"

# ─────────────────────────────────────────────────────────────────────────
# Estimation de durée AVANT lancement (médiane courante x tâches x reps) —
# voir ESTIMATE_CACHE_PATH dans test_web_tasks.py, mis à jour à la fin de
# CHAQUE campagne précédente (smoke ou complète) ; ce n'est qu'un cache
# glissant d'estimation, pas un historique (voir champ "_note" du fichier
# et campaign_persistence.py pour le véritable historique par campagne).
# Défaut 150s/tâche pour
# une tâche jamais mesurée (ordre de grandeur observé sur les campagnes
# passées, voir docs/history.md) — approximatif par construction, sert à choisir
# smoke vs complète en connaissance de cause, pas à garantir un temps exact.
# ─────────────────────────────────────────────────────────────────────────
ALL_TASK_IDS=(T1_extraction_paginee T2_formulaire_conge T3_tableau_dynamique
  T4_recherche_multi_sauts T5_telechargement_calcul T6_session_authentifiee
  T7_impossible_par_construction T8_wikipedia T9_google_insee
  T10_books_toscrape T11_sonde_peremption)
# Famille F seule pour l'instant (docs/briefs/B3-benchmark-v2.md) — garder
# en synchronisation manuelle avec FAMILY_F_TASK_IDS, test_web_tasks_v2.py.
V2_TASK_IDS=(T3_tableau_dynamique T5_telechargement_calcul T6_session_authentifiee T10_books_toscrape)
if [[ "$SUITE" == "v2" ]]; then
  ALL_TASK_IDS=("${V2_TASK_IDS[@]}")
fi

if [[ -n "$RESUME_CID" ]]; then
  # Reprise (Part 2.3) : REPS/TASKS/LABEL n'ont pas de sens ici (la liste
  # exacte des runs restants vient de planned[len(completed):], déjà
  # persistée) — affiche juste combien de runs restent plutôt que
  # l'estimation par tâche du lancement initial.
  PROGRESS_PATH="$CAMPAIGNS_DIR/${RESUME_CID}.progress.json"
  if [[ ! -f "$PROGRESS_PATH" ]]; then
    echo "Campagne introuvable : $PROGRESS_PATH" >&2
    exit 1
  fi
  "$VENV_PYTHON" -c "
import json
state = json.load(open('$PROGRESS_PATH'))
remaining = state['total_runs'] - len(state['completed'])
print(f'--- Reprise de {state[\"campaign_id\"]!r} : {remaining}/{state[\"total_runs\"]} runs restants (segment {len(state.get(\"segments\", []))}) ---')
"
else

"$VENV_PYTHON" - "$STATS_PATH" "$REPS" "$TASKS" "${ALL_TASK_IDS[@]}" <<'PYEOF'
import json
import sys

stats_path, reps, tasks_filter, *all_tasks = sys.argv[1:]
reps = int(reps)
prefixes = [p for p in tasks_filter.split(",") if p]

try:
    with open(stats_path, encoding="utf-8") as f:
        stats = json.load(f).get("estimates", {})
except (OSError, ValueError):
    stats = {}

DEFAULT_ESTIMATE_SECONDS = 150


def normalize(value):
    # Pre-B2 cache entries are a bare float — see _normalize_estimate,
    # test_web_tasks.py (docs/briefs/B2-campaign-control.md, Part 1.4).
    if isinstance(value, dict):
        return value
    return {"median": value, "min": value, "max": value, "n": 1}


selected = [t for t in all_tasks if not prefixes or any(t == p or t.startswith(p + "_") for p in prefixes)]
entries = {t: normalize(stats[t]) if t in stats else None for t in selected}
total_median = sum((entries[t] or {"median": DEFAULT_ESTIMATE_SECONDS})["median"] for t in selected) * reps
total_min = sum((entries[t] or {"min": DEFAULT_ESTIMATE_SECONDS})["min"] for t in selected) * reps
total_max = sum((entries[t] or {"max": DEFAULT_ESTIMATE_SECONDS})["max"] for t in selected) * reps

print(f"--- Estimation ({len(selected)} tache(s) x {reps} repetition(s) = {len(selected) * reps} runs) ---")
for t in selected:
    e = entries[t]
    if e is None:
        print(f"  {t:32s} {DEFAULT_ESTIMATE_SECONDS:6.1f}s (jamais mesuree, defaut)")
    else:
        spread = f" (min {e['min']:.1f}s / max {e['max']:.1f}s, n={e['n']})" if e["min"] != e["max"] else ""
        print(f"  {t:32s} {e['median']:6.1f}s{spread}")
print(
    f"--- Duree totale estimee : ~{total_median / 60:.0f} min "
    f"(plage {total_min / 60:.0f}-{total_max / 60:.0f} min, {total_median:.0f}s) ---"
)
PYEOF
fi

# ─────────────────────────────────────────────────────────────────────────
# Campagne : préambule (readiness LLM réelle + schéma d'outils) -> runs ->
# rapport écrit -> stats de durée mises à jour — tout depuis
# test_web_tasks_baseline (campaign_preflight.run_preflight en tête, voir
# ce module). RUN_LIVE_AGENT_TESTS=1 lève le skip d'opt-in.
# ─────────────────────────────────────────────────────────────────────────
export RUN_LIVE_AGENT_TESTS=1
if [[ "$SUITE" == "v2" ]]; then
  export WEB_TASKS_V2_REPORT_PATH="$REPORT_PATH"
  export WEB_TASKS_V2_REPETITIONS="$REPS"
  [[ -n "$LABEL" ]] && export WEB_TASKS_V2_CAMPAIGN_LABEL="$LABEL"
  [[ -n "$RESUME_CID" ]] && export WEB_TASKS_V2_RESUME_CAMPAIGN_ID="$RESUME_CID"
  PYTEST_TARGET="tests_integration/test_web_tasks_v2.py::test_web_tasks_v2_baseline"
else
  export WEB_TASKS_REPORT_PATH="$REPORT_PATH"
  if [[ -n "$RESUME_CID" ]]; then
    export WEB_TASKS_RESUME_CAMPAIGN_ID="$RESUME_CID"
  else
    export WEB_TASKS_REPETITIONS="$REPS"
    export WEB_TASKS_SMOKE_TASKS="$TASKS"
    [[ -n "$LABEL" ]] && export WEB_TASKS_CAMPAIGN_LABEL="$LABEL"
  fi
  PYTEST_TARGET="tests_integration/test_web_tasks.py::test_web_tasks_baseline"
fi

cd "$AGENT_DIR"
STATUS=0
"$VENV_PYTHON" -m pytest "$PYTEST_TARGET" -q -s -p no:cacheprovider \
  || STATUS=$?

# Pause propre (CAMPAIGN_PAUSED_EXIT_CODE, test_web_tasks.py Part 2.1) :
# PAS un échec — aucun rapport Markdown/fichier DONE à produire (la
# campagne n'est pas terminée), juste un message et une sortie 0.
if [[ "$STATUS" == "75" ]]; then
  echo "--- Campagne mise en pause (voir progress.json sous $CAMPAIGNS_DIR) — reprendre avec --resume ---"
  exit 0
fi

# ─────────────────────────────────────────────────────────────────────────
# Notification de fin — défaut : fichier DONE à côté du rapport (zéro
# dépendance externe, toujours disponible). ntfy en plus si NTFY_TOPIC est
# défini (curl vers ntfy.sh, best-effort — un échec réseau n'écrase jamais
# le fichier DONE). mail en plus si MAIL_TO est défini ET que la commande
# `mail` existe.
# ─────────────────────────────────────────────────────────────────────────
DONE_PATH="${REPORT_PATH%.md}.DONE"
SCORE_PATTERN='^\*\*Score de campagne'
[[ "$SUITE" == "v2" ]] && SCORE_PATTERN='^\*\*Alarmes'
SCORE_LINE="$(grep -m1 "$SCORE_PATTERN" "$REPORT_PATH" 2>/dev/null || echo "(rapport absent, voir STATUS=$STATUS)")"
{
  echo "Campagne terminée : $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "Statut pytest : $STATUS"
  echo "Rapport : $REPORT_PATH"
  echo "$SCORE_LINE"
} > "$DONE_PATH"
echo "--- $(cat "$DONE_PATH") ---"

if [[ -n "${NTFY_TOPIC:-}" ]]; then
  curl -fsS -m 10 -d "$(cat "$DONE_PATH")" "https://ntfy.sh/$NTFY_TOPIC" >/dev/null 2>&1 \
    || echo "avertissement : notification ntfy échouée (réseau ?)" >&2
fi

if [[ -n "${MAIL_TO:-}" ]] && command -v mail &>/dev/null; then
  mail -s "Campagne terminée ($STATUS)" "$MAIL_TO" < "$DONE_PATH" \
    || echo "avertissement : notification mail échouée" >&2
fi

exit "$STATUS"
