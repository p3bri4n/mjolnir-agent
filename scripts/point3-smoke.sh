#!/usr/bin/env bash
# One-off live-smoke helper for effort 2 point 3 (merged planning,
# PLANNING_MODE="merged" — docs/briefs/update-plan.md "2.1 addendum").
# The CAMPAIGN_EXPECTED_FLAGS_OVERRIDE JSON lives here instead of being
# retyped/copy-pasted through a chat UI each time — that path kept
# getting corrupted by a stray space/newline inserted on copy, silently
# turning a JSON key into a different key and defeating the override.
#
# Assumes langgraph-agent is ALREADY running with PLANNING_MODE=merged
# and the 4 legacy flags off (confirmed via `docker exec langgraph-agent
# env`) — this script does not force-recreate or start fixtures, it only
# repeats the smoke against different tasks on the already-configured
# container. Re-run the full docker compose sequence yourself first if
# the container's config may have drifted since.
#
# Delete this script once the point-3 build is fully validated (smoke +
# full sweep) — it's a one-off patch, not permanent tooling (CLAUDE.md,
# Scripts).
#
# Usage: bash scripts/point3-smoke.sh <task_id> [reps]
#   bash scripts/point3-smoke.sh A1_reconciliation_croisee
#   bash scripts/point3-smoke.sh A4_parcours_guide 2
set -euo pipefail

TASK="${1:?usage: point3-smoke.sh <task_id> [reps]}"
REPS="${2:-1}"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

export CAMPAIGN_EXPECTED_FLAGS_OVERRIDE='{"PLANNER_ENABLED":"false","VERIFICATION_ENABLED":"false","PLAN_VALIDATION_ENABLED":"false","PLAN_JUDGE_ENABLED":"false","PLANNING_MODE":"merged"}'

scripts/run-campaign.sh --suite v2 --tasks "$TASK" --reps "$REPS" --label "point3-smoke-${TASK}"
