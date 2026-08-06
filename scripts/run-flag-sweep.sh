#!/usr/bin/env bash
# Generic env-var sweep driver: runs scripts/run-campaign.sh once per
# configuration below, handling the force-recreate/readiness/preflight-
# override dance each one needs. Not a CLI on purpose — sweeps differ
# campaign to campaign (different vars, different task subset), so this
# is meant to be copied or edited in place per sweep rather than grown
# into a generic tool ahead of need. First use: docs/briefs/
# scaffolding-optimisation.md, "Effort 1 — Factorial ablation of the
# cognitive-core flags" (superseded scripts/run-ablation-effort2.sh, its
# one-off predecessor).
#
# CONFIGS entries: "label:VAR1=val1,VAR2=val2,..." — every VAR is
# exported and applied via `docker compose up -d --force-recreate
# langgraph-agent` before that run. Any VAR listed in
# PREFLIGHT_CHECKED_VARS is also folded into CAMPAIGN_EXPECTED_FLAGS_OVERRIDE
# (services/langgraph-agent/tests_integration/campaign_preflight.py) so
# preflight compares against the run's INTENDED value instead of
# rejecting every non-default config as flag drift. A VAR not in that
# list (e.g. NEVER_GRANTABLE_TOOLS_EXTRA) is exported to the container
# as usual but never added to the override — preflight doesn't check it,
# adding it anyway would create a bogus expected key preflight never
# fetches and always reads back as "".
#
# Usage: edit the block below, then: bash scripts/run-flag-sweep.sh
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"
AGENT_DIR="$PROJECT_DIR/services/langgraph-agent"

cd "$PROJECT_DIR"

# ─────────────────────────────────────────────────────────────────────────
# Edit per sweep — currently effort 2 point 3 (docs/history.md, "EFFORT 2"
# point 2/point 3): the discriminating-power subset (6 tasks, point 2) x
# 3 configs — cfg1/cfg8 (the decisive pair from the first 8-config sweep)
# plus cfg9, the 5th condition (merged planning, PLANNING_MODE="merged").
# n=3 (point 3's stated minimum). REQUIRES a live smoke (n=1, 1-2 tasks)
# BEFORE this full sweep — CLAUDE.md, "a live smoke precedes any final
# measurement of a family or mechanism": merged planning is a brand-new
# mechanism, never run live before.
# ─────────────────────────────────────────────────────────────────────────
SUITE="v2"
REPS="3"
TASKS="A1_reconciliation_croisee,A2_schema_references,A3_contact_conges,A4_parcours_guide,D1_cible_inexistante,B1_conge_hard"
LABEL_PREFIX="point3"
PREFLIGHT_CHECKED_VARS=(PLANNER_ENABLED VERIFICATION_ENABLED PLAN_VALIDATION_ENABLED PLAN_JUDGE_ENABLED PLANNING_MODE)

CONFIGS=(
  "cfg1-all-off:PLANNER_ENABLED=false,VERIFICATION_ENABLED=false,PLAN_VALIDATION_ENABLED=false,PLAN_JUDGE_ENABLED=false,PLANNING_MODE=nodes,NEVER_GRANTABLE_TOOLS_EXTRA=browser_click"
  "cfg8-all-on:PLANNER_ENABLED=true,VERIFICATION_ENABLED=true,PLAN_VALIDATION_ENABLED=true,PLAN_JUDGE_ENABLED=true,PLANNING_MODE=nodes,NEVER_GRANTABLE_TOOLS_EXTRA=browser_click"
  "cfg9-merged-planning:PLANNER_ENABLED=false,VERIFICATION_ENABLED=false,PLAN_VALIDATION_ENABLED=false,PLAN_JUDGE_ENABLED=false,PLANNING_MODE=merged,NEVER_GRANTABLE_TOOLS_EXTRA=browser_click"
)
# ─────────────────────────────────────────────────────────────────────────

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

# No healthcheck on langgraph-agent in docker-compose.yml (unlike most
# other services here) — `--force-recreate` returns as soon as the
# container starts, not once the FastAPI app has finished importing and
# is listening. Without this wait, preflight's tools/schema fetch races
# the app's own startup and fails ConnectionRefusedError.
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

is_preflight_checked() {
  local needle="$1" var
  for var in "${PREFLIGHT_CHECKED_VARS[@]}"; do
    [[ "$var" == "$needle" ]] && return 0
  done
  return 1
}

for entry in "${CONFIGS[@]}"; do
  label="${entry%%:*}"
  pairs="${entry#*:}"

  # Resume support: a config whose report already exists is done — skip
  # it rather than re-running (date-prefixed filename, so glob rather
  # than reconstruct today's date). Safe to re-launch this script after
  # any stop (clean --pause, Ctrl+C between configs, or a crash) without
  # re-measuring what's already on disk. Only whole-config granularity:
  # a config interrupted MID-run is not detected as done (no report yet)
  # and restarts from its own first task — use `run-campaign.sh --pause`
  # first if you need a clean mid-config stop instead.
  if compgen -G "$PROJECT_DIR/docs/campaigns/"*"_campaign-v2_${LABEL_PREFIX}-${label}.md" > /dev/null; then
    echo ""
    echo "=== $label — rapport déjà présent, skip ==="
    continue
  fi

  echo ""
  echo "=== $label ($pairs) ==="

  override_json="{"
  first=1
  IFS=',' read -ra kvs <<< "$pairs"
  for kv in "${kvs[@]}"; do
    var="${kv%%=*}"
    val="${kv#*=}"
    export "${var?}=${val}"
    if is_preflight_checked "$var"; then
      [[ $first -eq 0 ]] && override_json+=","
      override_json+="\"$var\":\"$val\""
      first=0
    fi
  done
  override_json+="}"

  docker compose up -d --force-recreate langgraph-agent
  wait_for_agent_ready

  export CAMPAIGN_EXPECTED_FLAGS_OVERRIDE="$override_json"
  scripts/run-campaign.sh --suite "$SUITE" --tasks "$TASKS" --reps "$REPS" --label "${LABEL_PREFIX}-${label}"
done

echo ""
echo "=== Sweep done — restoring the container to its default config ==="
for entry in "${CONFIGS[@]}"; do
  pairs="${entry#*:}"
  IFS=',' read -ra kvs <<< "$pairs"
  for kv in "${kvs[@]}"; do
    unset "${kv%%=*}" 2>/dev/null || true
  done
done
unset CAMPAIGN_EXPECTED_FLAGS_OVERRIDE
docker compose up -d --force-recreate langgraph-agent
wait_for_agent_ready
echo "=== Done. Reports under docs/campaigns/, prefix 2026-*_campaign-v2_${LABEL_PREFIX}-cfg*.md ==="
