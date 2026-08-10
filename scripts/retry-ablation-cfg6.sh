#!/usr/bin/env bash
# One-off follow-up to run-ablation-effort2.sh (docs/briefs/update-plan.md,
# Effort 2): the langgraph-agent container went down mid-campaign during
# cfg6-planner-verif-validation (docker exec: "container ... is not
# running", then "Connection refused" once it came back but wasn't ready
# yet) — see the investigation in this session, docs/campaigns/campaign-
# 20260804T154518Z-ablation-cfg6-planner-verif-validation.json, runs
# index 2-11. failure_cause="infra" on those 10/14 runs, not a real
# result for that flag combination.
#
# Retries ONLY the 5 tasks whose runs were voided (A1, A2, A4,
# B1_conge_hard, E3) — T3 and D1 already have valid cfg6 data and are not
# rerun. Same flags, same 2 repetitions as the original cfg6 leg.
#
# Delete this script once the retry lands in the effort-2 synthesis —
# it's a one-off patch, not permanent tooling (CLAUDE.md, Scripts).
#
# Usage: bash scripts/retry-ablation-cfg6.sh
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

TASKS="A1_reconciliation_croisee,A2_schema_references,A4_parcours_guide,B1_conge_hard,E3_routing_equivalence"

# cfg6-planner-verif-validation: PLANNER=true VERIFICATION=true
# PLAN_VALIDATION=true PLAN_JUDGE=false (docs/briefs/scaffolding-
# optimisation.md dependency constraint: judge inert without validation).
export PLANNER_ENABLED="true"
export VERIFICATION_ENABLED="true"
export PLAN_VALIDATION_ENABLED="true"
export PLAN_JUDGE_ENABLED="false"
export NEVER_GRANTABLE_TOOLS_EXTRA="browser_click"

docker compose up -d --force-recreate langgraph-agent
wait_for_agent_ready

export CAMPAIGN_EXPECTED_FLAGS_OVERRIDE="{\"PLANNER_ENABLED\":\"true\",\"VERIFICATION_ENABLED\":\"true\",\"PLAN_VALIDATION_ENABLED\":\"true\",\"PLAN_JUDGE_ENABLED\":\"false\"}"

scripts/run-campaign.sh --suite v2 --tasks "$TASKS" --reps 2 --label "ablation-cfg6-planner-verif-validation-retry"

echo ""
echo "=== Retry done — restoring the container to its default config (all flags true) ==="
unset PLANNER_ENABLED VERIFICATION_ENABLED PLAN_VALIDATION_ENABLED PLAN_JUDGE_ENABLED NEVER_GRANTABLE_TOOLS_EXTRA CAMPAIGN_EXPECTED_FLAGS_OVERRIDE
docker compose up -d --force-recreate langgraph-agent
wait_for_agent_ready
echo "=== Done. Report under docs/campaigns/, prefix 2026-*_campaign-v2_ablation-cfg6-planner-verif-validation-retry*.md ==="
echo "=== Merge with the original cfg6 report's T3/D1 rows for the effort-2 synthesis (both are valid). ==="
