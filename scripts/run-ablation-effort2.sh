#!/usr/bin/env bash
# One-off driver for effort 2.2 (docs/briefs/update-plan.md, "Effort 2" /
# docs/briefs/scaffolding-optimisation.md, "Effort 1 — Factorial ablation
# of the cognitive-core flags"): runs the 8 coherent configurations of the
# 4 cognitive-core flags (PLAN_VALIDATION_ENABLED inert without
# PLANNER_ENABLED, PLAN_JUDGE_ENABLED inert without PLAN_VALIDATION_ENABLED
# — see the checkpoint that fixed this subset) against the declared 7-task
# subset, 2 repetitions each, sequentially. Not meant to become permanent
# repo tooling — kept ad hoc for this one campaign, unlike scripts/run-
# campaign.sh which it wraps.
#
# Requires: services/langgraph-agent/tests_integration/campaign_preflight.py
# with CAMPAIGN_EXPECTED_FLAGS_OVERRIDE support (added alongside this
# script) — without it, preflight rejects every non-default config as
# "flag drift".
#
# Usage: bash scripts/run-ablation-effort2.sh
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"
AGENT_DIR="$PROJECT_DIR/services/langgraph-agent"

cd "$PROJECT_DIR"

# Single venv at project ROOT (docs/operations/testing.md), not per
# service — run-campaign.sh guesses $AGENT_DIR/.venv by default, which
# does not exist; export VENV_PYTHON so it picks up the real one too
# instead of silently falling back to a bare `python3` with no deps.
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "venv introuvable à $VENV_PYTHON — créez-le d'abord (docs/operations/testing.md)" >&2
  exit 1
fi
export VENV_PYTHON

echo "=== Sanity check: campaign_preflight unit tests ==="
( cd "$AGENT_DIR" && "$VENV_PYTHON" -m pytest tests/test_campaign_preflight.py -q )

# The 7-task subset spans every self-hosted fixture (catalog/docs/hr-app
# for A1/A2/A4/T3, admin for none here but started anyway — cheap, avoids
# a second profile flavor — perception for E3). Idempotent: a no-op if
# already up. Preflight's fixture-reachability check would otherwise
# fail every config identically (docs/history.md, the 2026-07-28 44-
# minute run against unreachable fixtures that motivated this check).
echo "=== Starting self-hosted fixtures (profile test-fixtures) ==="
docker compose --profile test-fixtures up -d fixture-catalog fixture-docs fixture-hr-app fixture-admin fixture-perception

# No healthcheck on langgraph-agent in docker-compose.yml (unlike most
# other services here) — `--force-recreate` returns as soon as the
# container starts, not once the FastAPI app has finished importing and
# is listening. Without this wait, preflight's tools/schema fetch
# (localhost:8000, via docker exec) races the app's own startup and
# fails ConnectionRefusedError — same class of race wait_for_llm_ready
# already guards against for TabbyAPI, just missing here because that
# check only covers the LLM backend, not the agent container itself.
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

# 7-task subset declared at the checkpoint before any result was seen:
# long horizon (A1, A2, A4), short (F: T3, D: D1), policy (B1 hard),
# perception (E3).
TASKS="A1_reconciliation_croisee,A2_schema_references,A4_parcours_guide,T3_tableau_dynamique,D1_cible_inexistante,B1_conge_hard,E3_routing_equivalence"

# label:PLANNER:VERIFICATION:PLAN_VALIDATION:PLAN_JUDGE
CONFIGS=(
  "cfg1-all-off:false:false:false:false"
  "cfg2-verif-only:false:true:false:false"
  "cfg3-planner-only:true:false:false:false"
  "cfg4-planner-verif:true:true:false:false"
  "cfg5-planner-validation:true:false:true:false"
  "cfg6-planner-verif-validation:true:true:true:false"
  "cfg7-planner-validation-judge:true:false:true:true"
  "cfg8-all-on:true:true:true:true"
)

for entry in "${CONFIGS[@]}"; do
  IFS=":" read -r label planner verif validation judge <<< "$entry"
  echo ""
  echo "=== $label — PLANNER=$planner VERIFICATION=$verif PLAN_VALIDATION=$validation PLAN_JUDGE=$judge ==="

  export PLANNER_ENABLED="$planner"
  export VERIFICATION_ENABLED="$verif"
  export PLAN_VALIDATION_ENABLED="$validation"
  export PLAN_JUDGE_ENABLED="$judge"
  # B1_conge_hard is in every config's subset — required every time, not
  # just for the all-on config (docs/briefs/B3-benchmark-v2.md checkpoint
  # 2026-07-30; omitting it silently degrades hard's CuP judge, already
  # mistaken for a policy bug once, see docs/history.md).
  export NEVER_GRANTABLE_TOOLS_EXTRA="browser_click"

  docker compose up -d --force-recreate langgraph-agent
  wait_for_agent_ready

  # Preflight compares effective container flags against this override
  # instead of the "all true" default — read host-side by pytest, no
  # container restart needed for this one.
  export CAMPAIGN_EXPECTED_FLAGS_OVERRIDE="{\"PLANNER_ENABLED\":\"$planner\",\"VERIFICATION_ENABLED\":\"$verif\",\"PLAN_VALIDATION_ENABLED\":\"$validation\",\"PLAN_JUDGE_ENABLED\":\"$judge\"}"

  scripts/run-campaign.sh --suite v2 --tasks "$TASKS" --reps 2 --label "ablation-${label}"
done

echo ""
echo "=== Ablation done — restoring the container to its default config (all flags true) ==="
unset PLANNER_ENABLED VERIFICATION_ENABLED PLAN_VALIDATION_ENABLED PLAN_JUDGE_ENABLED NEVER_GRANTABLE_TOOLS_EXTRA CAMPAIGN_EXPECTED_FLAGS_OVERRIDE
docker compose up -d --force-recreate langgraph-agent
wait_for_agent_ready
echo "=== Done. 8 reports under docs/campaigns/, prefix 2026-*_campaign-v2_ablation-cfg*.md ==="
