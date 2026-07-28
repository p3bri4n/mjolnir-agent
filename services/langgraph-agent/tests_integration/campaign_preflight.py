"""
Campaign preamble (Iteration 0, docs/briefs/phase-1-coeur-cognitif.md):
before launching a harness campaign (test_web_tasks.py), checks that the
tool schema actually seen by langgraph-agent matches what's expected AND
what mcp-client serves at that same instant, then forces a clean starting
state (browser session reset, downloads volume purge). A gap raises
PreflightError BEFORE the campaign's first run — never a run that starts
then fails for an infra reason that was already detectable.

Raison d'être (lesson from the "tool-schema cache bug", see
docs/history.md, revised Phase 1d): `_tools_schema_cache` (app/graph.py)
is filled once for the langgraph-agent process's lifetime and is NEVER
invalidated. Restarting mcp-client alone (new tool added/schema updated
server-side) can therefore leave langgraph-agent running with a stale
view, silently — a first full-campaign attempt in revised Phase 1d ran
entirely on a frozen schema before `browser_extract` was even really
active, invalidating the whole run with no error flagging it on the
spot. This module makes this class of bug detectable BEFORE spending a
whole campaign on it.

EXPECTED_TOOLS is NOT an attempt at an exhaustive enumeration of the
schema (most browser_* tools come from the official mcp/playwright
image, whose exact tool names aren't maintained in this repo — guessing
them would violate the rule "any claim about a library's behavior is
verified against the installed code", CLAUDE.md #8). It's therefore
limited to the union of tools already named elsewhere in THIS repo: the
tiers from app/approval_policy.py (already the maintained reference
config) + browser_navigate (the only browser_* tool name literally
referenced in app/graph.py, via the URL-fabrication guardrail).
"""

import json
import subprocess
import time
from typing import Callable, Iterable, Optional

import app.approval_policy as policy
from tests_integration import campaign_persistence

AGENT_CONTAINER = "langgraph-agent"
MCP_CLIENT_CONTAINER = "mcp-client"
TABBYAPI_CONTAINER = "tabbyapi"
TABBYAPI_IMAGE_TAG = "mjolnir-agent-tabbyapi"

# LLM readiness (campaign tooling, see docs/history.md): found under real
# conditions — a `docker compose up --build langgraph-agent` also
# recreated tabbyapi (config drift detected); the campaign started ~20s
# after "Model successfully loaded" but BEFORE the HTTP server was
# actually listening, producing 30 near-instant failures
# (openai.APIConnectionError, captured as an internal error notice)
# before any assertion could reveal the problem. The previous preamble
# (check_tools_schema) checked ONLY the tool schema via mcp-client, never
# that the LLM backend actually answers a completion — blind spot now
# covered by wait_for_llm_ready.
LLM_READY_TIMEOUT_SECONDS = 180
LLM_READY_POLL_INTERVAL_SECONDS = 5

EXPECTED_TOOLS = policy.TIER_READ_TOOLS | policy.TIER_REVERSIBLE_TOOLS | policy.NEVER_GRANTABLE_TOOLS | {
    "browser_navigate"
}

# Self-hosted fixtures (docker-compose.yml, profile "test-fixtures") targeted
# by T1-T7 (docs/benchmark-v1.md): found missing 2026-07-28 (docs/campaigns/
# 2026-07-28_campaign_post-rename-mjolnir.md, invalid 14/33 run) — nothing
# checked the profile was up before launch, so a campaign ran 44 minutes
# against unreachable fixtures before anyone noticed. URLs as reachable from
# AGENT_CONTAINER (same agent-net network).
FIXTURE_URLS = {
    "fixture-catalog": "http://fixture-catalog/",
    "fixture-docs": "http://fixture-docs/",
    "fixture-hr-app": "http://fixture-hr-app:5000/",
}

# Effective flags control (docs/briefs/flags-du-coeur-cognitif.md, point
# 2): expected values INSIDE THE RUNNING CONTAINER — the 4 cognitive-core
# flags (default "true" now, see app/graph.py and docker-compose.yml) +
# the other variables that drive measured behavior (attempt/replan
# budgets, curbed thinking, tier overrides, truncation thresholds). List
# and values taken as-is from app/graph.py/app/approval_policy.py (never
# guessed) — see CAMPAIGN_ENV_FLAGS (campaign_persistence.py) for the
# same list of NAMES, reused here to avoid duplicating it. A value absent
# from the container (docker-compose.yml doesn't pass it in environment)
# compares to "" (empty string, never None — avoids a false type
# mismatch in the diff).
EXPECTED_AGENT_FLAGS = {
    "MAX_TOOL_ITERATIONS": "20",
    "LLM_MAX_TOKENS": "2048",
    "PLANNER_ENABLED": "true",
    "PLANNER_MAX_TOKENS": "8192",
    "PLANNER_THINKING_ENABLED": "false",
    "VERIFICATION_ENABLED": "true",
    "SUBTASK_ATTEMPT_BUDGET": "3",
    "REPLAN_BUDGET": "2",
    "PLAN_VALIDATION_ENABLED": "true",
    "PLAN_JUDGE_ENABLED": "true",
    "ADAPTIVE_THINKING": "true",
    "MAX_IMAGES_IN_CONTEXT": "1",
    "IMAGE_FORMAT_PASSTHROUGH": "",
    "IMAGE_TOKEN_ESTIMATE": "1500",
    "AUTO_APPROVAL_STREAK_LIMIT": "6",
    "AUTO_APPROVED_TOOLS": "",
    "APPROVAL_RULES_PATH": "",
    "BROWSER_TOOL_OUTPUT_MAX_CHARS": "8000",
    "AFFORDANCE_THRESHOLD": "60",
    "FABRICATION_LIMIT": "5",
    "BROWSER_NAVIGATE_GUARDRAIL": "true",
    "MAX_EMPTY_ANSWER_RETRIES": "1",
    "AUDIT_LOG_MAX_BYTES": str(20 * 1024 * 1024),
    "EPISODE_COMPACTION_ENABLED": "false",
    "EPISODE_COMPACTION_TURN_THRESHOLD": "40",
}


class PreflightError(RuntimeError):
    """Raised by run_preflight(): the campaign must NOT start."""


def check_tools_schema(agent_tools: Iterable[str], mcp_tools: Iterable[str]) -> Optional[str]:
    """
    Pure, unit-testable without docker: None if all is well, otherwise a
    message explaining the rejection (compared BEFORE expected, since a
    desync between the two services makes any conclusion about "the
    expected" misleading until it's resolved).
    """
    agent_tools = set(agent_tools)
    mcp_tools = set(mcp_tools)
    if agent_tools != mcp_tools:
        missing_in_agent = sorted(mcp_tools - agent_tools)
        extra_in_agent = sorted(agent_tools - mcp_tools)
        return (
            "schéma d'outils désynchronisé entre langgraph-agent et mcp-client "
            f"(absents côté langgraph-agent={missing_in_agent}, superflus côté "
            f"langgraph-agent={extra_in_agent}) — _tools_schema_cache est probablement "
            "périmé, commande à taper : docker compose restart langgraph-agent"
        )
    missing_expected = sorted(EXPECTED_TOOLS - agent_tools)
    if missing_expected:
        return f"outils attendus absents du schéma effectif de langgraph-agent : {missing_expected}"
    return None


def check_agent_flags(actual_flags: dict) -> Optional[str]:
    """
    Pure, unit-testable without docker (see check_tools_schema above,
    same style): None if `actual_flags` (see _fetch_agent_env below)
    exactly matches EXPECTED_AGENT_FLAGS for every expected key, otherwise
    a message listing the diff (key, expected, actual) — a campaign
    measured against a drifted flag (e.g. a local .env still overriding
    the old "false" default) must never claim to be comparable to the
    reference campaign without flagging it BEFORE the first run. A key
    absent from `actual_flags` (docker exec returned nothing, e.g. a
    container not restarted since a variable was added to
    docker-compose.yml) compares to "" as an empty value, never ignored.
    """
    diffs = []
    for key, expected in EXPECTED_AGENT_FLAGS.items():
        actual = actual_flags.get(key, "")
        if actual != expected:
            diffs.append(f"{key} : attendu={expected!r} effectif={actual!r}")
    if diffs:
        return (
            "flags d'env effectifs de langgraph-agent différents de la config mesurée "
            f"({'; '.join(diffs)}) — commande à taper si un changement de .env n'a pas "
            "encore été appliqué : docker compose up -d --force-recreate langgraph-agent"
        )
    return None


def check_fixtures_reachable(reachability: dict) -> Optional[str]:
    """
    Pure, unit-testable without docker (same style as check_tools_schema):
    None if `reachability` (see _fetch_fixtures_reachable below) marks every
    FIXTURE_URLS entry as reachable, otherwise a message listing which
    aren't, with the profile start command.
    """
    unreachable = sorted(name for name in FIXTURE_URLS if not reachability.get(name, False))
    if unreachable:
        return (
            f"fixtures self-hosted injoignables depuis {AGENT_CONTAINER} : {unreachable} — "
            "commande à taper : docker compose --profile test-fixtures up -d "
            "fixture-catalog fixture-docs fixture-hr-app"
        )
    return None


def _docker_exec_python(container: str, script: str, timeout: int = 30) -> str:
    result = subprocess.run(
        ["docker", "exec", "-i", container, "python3", "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise PreflightError(f"docker exec dans {container} a échoué (préambule) : {result.stderr}")
    return result.stdout


def _fetch_agent_tools() -> list:
    script = """
import urllib.request
with urllib.request.urlopen('http://localhost:8000/tools/schema', timeout=10) as r:
    print(r.read().decode())
"""
    return json.loads(_docker_exec_python(AGENT_CONTAINER, script)).get("tools", [])


def _fetch_mcp_tools() -> list:
    script = """
import json, urllib.request
with urllib.request.urlopen('http://localhost:8003/tools/schema', timeout=10) as r:
    body = json.loads(r.read().decode())
print(json.dumps(sorted({t["function"]["name"] for t in body.get("tools", [])})))
"""
    return json.loads(_docker_exec_python(MCP_CLIENT_CONTAINER, script))


def _fetch_llm_ready() -> bool:
    """
    REAL completion call (not a /health) against LLM_BASE_URL as seen by
    langgraph-agent itself (portable to the alternative llama-server
    backend, see README "Inference backend" — not just TabbyAPI): this
    is the only check that would have caught the real-conditions case
    found (server not yet listening despite a model already loaded).
    enable_thinking=False + max_tokens=1: as fast as possible, we only
    want a finish_reason, not a real answer.
    """
    script = """
import json, os, urllib.request, urllib.error
base = os.environ.get('LLM_BASE_URL', 'http://tabbyapi:5000/v1').rstrip('/')
req = urllib.request.Request(
    base + '/chat/completions',
    data=json.dumps({
        'model': 'agent-llm',
        'messages': [{'role': 'user', 'content': 'ping'}],
        'max_tokens': 1,
        'enable_thinking': False,
    }).encode(),
    headers={'Content-Type': 'application/json'},
)
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        print(r.status)
except Exception as e:
    print('ERROR', repr(e))
"""
    out = _docker_exec_python(AGENT_CONTAINER, script, timeout=15)
    return out.strip() == "200"


def _fetch_fixtures_reachable() -> dict:
    """Real HTTP GET (docker exec + urllib, same primitive as
    _fetch_llm_ready) against each of FIXTURE_URLS from inside
    AGENT_CONTAINER — {name: True/False}, never raises on an individual
    unreachable fixture (that's exactly the condition being checked)."""
    script = f"""
import json, urllib.request
urls = {FIXTURE_URLS!r}
result = {{}}
for name, url in urls.items():
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            result[name] = r.status == 200
    except Exception:
        result[name] = False
print(json.dumps(result))
"""
    return json.loads(_docker_exec_python(AGENT_CONTAINER, script))


def _run_docker(args: list, timeout: int = 15) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise PreflightError(f"`docker {' '.join(args)}` a échoué (préambule) : {result.stderr.strip()}")
    return result.stdout.strip()


def _fetch_tabbyapi_image_ids() -> tuple:
    """(id of the image ACTUALLY used by the running tabbyapi container,
    id of the last locally built image for this tag) — see
    check_tabbyapi_image_fresh."""
    running = _run_docker(["inspect", "--format", "{{.Image}}", TABBYAPI_CONTAINER])
    built = _run_docker(["image", "inspect", "--format", "{{.Id}}", TABBYAPI_IMAGE_TAG])
    return running, built


def check_tabbyapi_image_fresh(fetch_image_ids: Callable[[], tuple] = _fetch_tabbyapi_image_ids) -> Optional[str]:
    """
    Image digest check (post-1/2-ter arbitration, see docs/history.md,
    action 1): detects a tabbyapi container running on an image
    DIFFERENT from the last one built locally for this tag — e.g.
    `docker compose build` run without the `up -d` that applies the
    change, or a forgotten manual image rollback. Such a gap would let a
    whole campaign run against a different model/version than expected,
    silently (no error, just different behavior) — the same class of
    risk as the tool-schema desync that check_tools_schema already
    detects on the langgraph-agent/mcp-client side. Pure once
    fetch_image_ids is injected (see tests/test_campaign_preflight.py):
    no real docker in the tests.
    """
    running_id, built_id = fetch_image_ids()
    if running_id != built_id:
        return (
            f"le conteneur {TABBYAPI_CONTAINER} tourne sur une image différente de la dernière "
            f"construite pour {TABBYAPI_IMAGE_TAG} (running={running_id}, built={built_id}) — "
            "commande à taper : docker compose up -d --build tabbyapi"
        )
    return None


def _fetch_agent_env() -> dict:
    """Effective flags INSIDE the running langgraph-agent container — see
    check_agent_flags. Reuses campaign_persistence.collect_env_flags (the
    same `docker exec ... env` primitive as campaign serialization, see
    campaign_persistence.py) rather than duplicating a variant here."""
    return campaign_persistence.collect_env_flags(AGENT_CONTAINER, list(EXPECTED_AGENT_FLAGS))


def wait_for_llm_ready(
    fetch_llm_ready: Callable[[], bool] = _fetch_llm_ready,
    *,
    timeout_seconds: int = LLM_READY_TIMEOUT_SECONDS,
    interval_seconds: int = LLM_READY_POLL_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> None:
    """
    Polls fetch_llm_ready until success or timeout — see
    LLM_READY_TIMEOUT_SECONDS above for the rationale. `sleep`/`now`
    injectable for a fast unit test (see tests/test_campaign_preflight.py),
    with no real delay or docker.
    """
    deadline = now() + timeout_seconds
    while not fetch_llm_ready():
        if now() >= deadline:
            raise PreflightError(
                f"LLM_BASE_URL ne répond pas à une complétion réelle après {timeout_seconds}s "
                "— vérifier `docker logs tabbyapi` (serveur pas encore démarré, ou crash au chargement)"
            )
        sleep(interval_seconds)


def run_preflight(
    *,
    purge_downloads: Callable[[], None],
    reset_browser_session: Callable[[], None],
    fetch_agent_tools: Callable[[], Iterable[str]] = _fetch_agent_tools,
    fetch_mcp_tools: Callable[[], Iterable[str]] = _fetch_mcp_tools,
    fetch_llm_ready: Callable[[], bool] = _fetch_llm_ready,
    fetch_tabbyapi_image_ids: Callable[[], tuple] = _fetch_tabbyapi_image_ids,
    fetch_agent_env: Callable[[], dict] = _fetch_agent_env,
    fetch_fixtures_reachable: Callable[[], dict] = _fetch_fixtures_reachable,
) -> None:
    """
    Called ONCE per campaign (not per repetition, unlike
    purge_downloads/reset_browser_session which also stay called before
    each individual repetition — see test_web_tasks.py). Fetch callables
    injectable to allow a full unit test of the orchestration with no
    docker (see tests/test_campaign_preflight.py); purge_downloads/
    reset_browser_session remain mandatory parameters rather than
    internal defaults so as never to duplicate their implementation
    (already in test_web_tasks.py, with their own documented rationale).

    Order: LLM readiness FIRST (cheapest to observe IN ERROR — no point
    comparing tool schemas if the backend doesn't even respond), then
    tabbyapi image freshness (post-1/2-ter arbitration, see
    docs/history.md), then effective env flags (docs/briefs/
    flags-du-coeur-cognitif.md — no point measuring a campaign against a
    config we don't actually have), then tool schema, then fixture
    reachability (docs/campaigns/2026-07-28_campaign_post-rename-mjolnir.md
    — a campaign against unreachable T1-T7 fixtures wastes a full run
    before failing on assertions, not before starting), then purge/reset.
    """
    wait_for_llm_ready(fetch_llm_ready)
    error = check_tabbyapi_image_fresh(fetch_tabbyapi_image_ids)
    if error:
        raise PreflightError(error)
    error = check_agent_flags(fetch_agent_env())
    if error:
        raise PreflightError(error)
    error = check_tools_schema(fetch_agent_tools(), fetch_mcp_tools())
    if error:
        raise PreflightError(error)
    error = check_fixtures_reachable(fetch_fixtures_reachable())
    if error:
        raise PreflightError(error)
    purge_downloads()
    reset_browser_session()
