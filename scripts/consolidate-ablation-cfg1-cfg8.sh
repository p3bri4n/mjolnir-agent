#!/usr/bin/env bash
# One-off follow-up to run-ablation-effort2.sh (docs/briefs/update-plan.md,
# Effort 2): checkpoint decision 2026-08-05 (docs/history.md, "EFFORT 2")
# reads cfg1-all-off and cfg8-all-on as TIED on the frozen CuP judge
# (12/14 each, n=2/task) while every intermediate config scores below
# both — but at n=2/task a single flipped run moves a config's score by
# ~7%, so the tie isn't confirmed at this power. This script adds 3 more
# repetitions to EACH of cfg1 and cfg8 only (not the other 6 configs —
# they're not what the removal decision hinges on), same 7-task subset,
# same preamble as the original campaign. Merge with the original n=2 for
# n=5 total per task before re-reading the decision table.
#
# Delete this script once the consolidation lands in the effort-2
# synthesis — it's a one-off patch, not permanent tooling (CLAUDE.md,
# Scripts).
#
# Usage: bash scripts/consolidate-ablation-cfg1-cfg8.sh
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"
AGENT_DIR="$PROJECT_DIR/services/langgraph-agent"

cd "$PROJECT_DIR"

VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "venv introuvable à $VENV_PYTHON — créez-le d'abord (docs/operations/testing.md)" >&2
  exit 1
fi
export VENV_PYTHON

echo "=== Sanity check: campaign_preflight unit tests ==="
( cd "$AGENT_DIR" && "$VENV_PYTHON" -m pytest tests/test_campaign_preflight.py -q )

echo "=== Starting self-hosted fixtures (profile test-fixtures) ==="
docker compose --profile test-fixtures up -d fixture-catalog fixture-docs fixture-hr-app fixture-admin fixture-perception

# Same race as run-ablation-effort2.sh: --force-recreate returns as soon
# as the container starts, not once FastAPI is listening.
wait_for_agent_ready() {
  local waited=0 timeout=90 interval=3
  until docker exec langgraph-agent python3 -c \
    "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)" \
    &>/dev/null; do
    if (( waited >= timeout )); then
      echo "langgraph-agent ne répond pas sur /health après ${timeout}s — voir docker logs langgraph-agent" >&2
      exit 1
    fi
    sleep "$interval"
    waited=$((waited + interval))
  done
}

# Same 7-task subset declared at the original effort-2 checkpoint.
TASKS="A1_reconciliation_croisee,A2_schema_references,A4_parcours_guide,T3_tableau_dynamique,D1_cible_inexistante,B1_conge_hard,E3_routing_equivalence"

# label:PLANNER:VERIFICATION:PLAN_VALIDATION:PLAN_JUDGE — only the two
# bookends of the original 8-config matrix.
CONFIGS=(
  "cfg1-all-off:false:false:false:false"
  "cfg8-all-on:true:true:true:true"
)

for entry in "${CONFIGS[@]}"; do
  IFS=":" read -r label planner verif validation judge <<< "$entry"
  echo ""
  echo "=== $label consolidate (+3 reps) — PLANNER=$planner VERIFICATION=$verif PLAN_VALIDATION=$validation PLAN_JUDGE=$judge ==="

  export PLANNER_ENABLED="$planner"
  export VERIFICATION_ENABLED="$verif"
  export PLAN_VALIDATION_ENABLED="$validation"
  export PLAN_JUDGE_ENABLED="$judge"
  export NEVER_GRANTABLE_TOOLS_EXTRA="browser_click"

  docker compose up -d --force-recreate langgraph-agent
  wait_for_agent_ready

  export CAMPAIGN_EXPECTED_FLAGS_OVERRIDE="{\"PLANNER_ENABLED\":\"$planner\",\"VERIFICATION_ENABLED\":\"$verif\",\"PLAN_VALIDATION_ENABLED\":\"$validation\",\"PLAN_JUDGE_ENABLED\":\"$judge\"}"

  scripts/run-campaign.sh --suite v2 --tasks "$TASKS" --reps 3 --label "ablation-${label}-consolidate"
done

echo ""
echo "=== Consolidation done — restoring the container to its default config (all flags true) ==="
unset PLANNER_ENABLED VERIFICATION_ENABLED PLAN_VALIDATION_ENABLED PLAN_JUDGE_ENABLED NEVER_GRANTABLE_TOOLS_EXTRA CAMPAIGN_EXPECTED_FLAGS_OVERRIDE
docker compose up -d --force-recreate langgraph-agent
wait_for_agent_ready
echo "=== Done. 2 reports under docs/campaigns/, prefix 2026-*_campaign-v2_ablation-cfg1-all-off-consolidate*.md and ablation-cfg8-all-on-consolidate*.md ==="
echo "=== Merge each with its original 2-rep data (n=5 total/task) before re-reading the decision table. ==="
