"""
Web-task harness (Phase 0 of the autonomy plan, see PLAN.md and
docs/benchmark-v1.md): replays 11 multi-step web tasks against the REAL
agent (Docker containers, real LLM, real Playwright browser), with a
PROGRAMMATIC success criterion per task — never a qualitative judgment.

Like test_tool_calling_baseline.py/test_semantic_drift.py: talks to the
real containers via `docker exec`, slow and non-deterministic by nature.
Skipped by default; explicit opt-in:

    RUN_LIVE_AGENT_TESTS=1 python -m pytest tests_integration/test_web_tasks.py -v

Prerequisites:
  - `docker compose up -d` (normal stack) AND
    `docker compose --profile test-fixtures up -d fixture-catalog fixture-docs
    fixture-hr-app` (see docker-compose.yml).
  - `browser_*` tools are TIER_SENSITIVE by default (see
    approval_policy.py — Phase 3 of the plan should change this, not done
    yet): this harness THEREFORE plays the human's role itself via
    POST /approve (with grant_session=True) to run a task with no
    manual intervention, and counts these approvals as a metric
    ("approval interventions" — see docs/benchmark-v1.md).

Recalibrations made while building this harness (see docs/history.md for
detail):
  - T1: catalog reduced from 120/12 pages to 30/3 pages — the worst-case
    exhaustive search (reference never visible in the listing) far
    exceeded MAX_TOOL_ITERATIONS at the initial scale.
  - T5: assertion on the final value (exact payroll total in the
    answer), not on a CSV file present in a directory — stays true even
    from the dedicated download volume (revised Phase 1d, see
    docker-compose.yml `agent-downloads` and docs/history.md): the agent
    must download THEN read via the filesystem tool under `/downloads/`
    (`fetch()`/`browser_evaluate` as a file-transfer channel was
    explicitly ruled out, see docs/history.md — that's not a read tool's
    primitive). `_purge_downloads_volume()` (see below) empties this
    volume before each repetition so a run never "succeeds" by reading a
    previous run's artifact.

Accepted known limitation of the "tokens consumed" metric
(docs/benchmark-v1.md): not measured by this harness —
`/v1/chat/completions` returns no `usage` field (verified in
app/main.py), and properly instrumenting it is out of scope for this
Phase 0.

Point-zero finding #1 (see smoke tests, docs/history.md): this harness's
first two dry runs (T1, T7) failed by hitting MAX_TOOL_ITERATIONS, in
both cases after a navigation to a URL FABRICATED by the model
(`page-4.html` — the catalog only has 3 pages — then
`/catalog/search?q=ZZ-9999` — no search exists on this fixture) rather
than followed from a link actually observed in the DOM. Two sub-causes
distinguished below so as not to conflate the two:

  - "boucle_fabrication": at least one navigation to a URL absent from
    the real site during the run (see `KNOWN_URLS_BY_TASK`/
    `_classify_boucle_subcause`) — the model invented a plausible path
    rather than following an observed link.
  - "boucle_budget": the model was making progress on real URLs but ran
    out of iterations.

Limitation of this sub-classification: it checks each navigated URL's
membership in the set of URLs ACTUALLY served by the fixture (computed
from the generators, ground truth already known) — NOT an exact
reconstruction of the DOM/snapshots seen by the model turn by turn
(tool_call results aren't logged, only the name and arguments are). A
false negative would therefore be a navigation to a URL that DOES EXIST
on the site but that the model never actually saw in a snapshot (just
guessed right). No sub-classification for tasks on real sites (T8-T10):
no reference sitemap available for these targets.
"""
import ast
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests_integration import campaign_persistence, campaign_preflight

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_AGENT_TESTS") != "1",
    reason="live integration test (real web agent): opt-in via RUN_LIVE_AGENT_TESTS=1, "
    "requires docker compose up + test-fixtures profile",
)

AGENT_CONTAINER = os.environ.get("LANGGRAPH_AGENT_CONTAINER", "langgraph-agent")
MCP_CLIENT_CONTAINER = os.environ.get("MCP_CLIENT_CONTAINER", "mcp-client")
N_REPETITIONS = int(os.environ.get("WEB_TASKS_REPETITIONS", "3"))
MAX_APPROVAL_ROUNDS = int(os.environ.get("WEB_TASKS_MAX_APPROVAL_ROUNDS", "40"))
CHAT_TIMEOUT_SECONDS = int(os.environ.get("WEB_TASKS_CHAT_TIMEOUT", "240"))
# Smoke mode (campaign tooling, see docs/history.md and run-campaign.sh):
# a subset of tasks (comma-separated prefixes, e.g. "T1,T7,T11" — matched
# against the start of task_id, no exact name required) to ITERATE
# quickly on a fix, with the SAME preamble/judges/report generation as
# the full campaign (_run_campaign/_write_report unchanged) — never a
# parallel suite to maintain separately. Protocol: smoke to develop/
# verify fast, full campaign (WEB_TASKS_SMOKE_TASKS unset, 3 repetitions)
# reserved for checkpoints that count toward a reference score — a smoke
# run lacks the statistical significance (reduced n) to arbitrate a
# pass/regression threshold.
SMOKE_TASK_PREFIXES = [
    p.strip() for p in os.environ.get("WEB_TASKS_SMOKE_TASKS", "").split(",") if p.strip()
]

FIXTURES_DIR = Path(__file__).parent / "fixtures"
for _sub in ("catalog", "docs", "hr-app"):
    sys.path.insert(0, str(FIXTURES_DIR / _sub))
import generate_catalog  # noqa: E402
import generate_docs  # noqa: E402
import hr_data  # noqa: E402

CATALOG_URL = "http://fixture-catalog/catalog"
DOCS_URL = "http://fixture-docs/docs"
HR_APP_URL = "http://fixture-hr-app:5000"

WORKSPACE_HOST_PATH = Path(
    os.environ.get("WORKSPACE_HOST_PATH", Path(__file__).parents[3] / "workspace")
)
HR_APP_DATA_FILE = WORKSPACE_HOST_PATH / "hr-app-data" / "leave_submissions.json"

# Report naming convention (Phase 2, restructuration+anglais):
# YYYY-MM-DD_type_label.md under docs/campaigns/ — run-campaign.sh builds
# this path itself and exports it via WEB_TASKS_REPORT_PATH; this default
# only serves a direct pytest launch bypassing the script.
CAMPAIGNS_DIR = Path(__file__).parents[3] / "docs" / "campaigns"
REPORT_PATH = Path(
    os.environ.get("WEB_TASKS_REPORT_PATH", CAMPAIGNS_DIR / f"{datetime.now(timezone.utc):%Y-%m-%d}_campaign_default.md")
)
CAMPAIGN_LABEL = os.environ.get("WEB_TASKS_CAMPAIGN_LABEL", "Campagne A (budget par défaut)")
# Campaign tooling (run-campaign.sh): current median duration per task,
# updated at the end of EVERY campaign (full or smoke) — lets a next
# launch's duration be estimated (tasks × repetitions × known median)
# BEFORE starting it, to choose smoke or full knowingly. Deliberately a
# single shared file: the last known measurement per task is the best
# available estimate, whether it came from a smoke or a full campaign.
# Explicitly named "estimate cache" (not "campaign stats"): this is NOT a
# history, every campaign overwrites its tasks' value — see the file's
# own "_note" field and campaign_persistence.py for the real per-campaign
# history (campaign-<timestamp>-<label>.json).
ESTIMATE_CACHE_PATH = Path(__file__).parent / "DURATION_ESTIMATE_CACHE.json"

# Exact texts emitted server-side (see app/main.py) — same convention as
# test_tool_calling_baseline.py.
_APPROVAL_PREFIX = "⚠️ Approbation requise pour"
# Plan validation pipeline (Iteration 3, Phase 1 "cognitive core" — see
# docs/briefs/phase-1-coeur-cognitif.md and app/main.py:
# _format_plan_approval_request): TWO additional possible pauses, at the
# PLAN level rather than a tool_call — normal tier-based approval, or
# human escalation after automatic validation failure. Without
# recognizing them, run_task() would treat these messages as a FINAL
# answer (they don't start with _APPROVAL_PREFIX), invalidating any
# campaign as soon as PLAN_VALIDATION_ENABLED is active with no error
# flagging it on the spot.
_PLAN_APPROVAL_PREFIX = "⚠️ Approbation du plan requise"
_PLAN_ESCALATION_PREFIX = "⚠️ Le plan proposé a été rejeté par la validation automatique"
_ITERATION_LIMIT_PREFIX = "⚠️ Limite d'itérations d'outils atteinte"
_EMPTY_NOTICE_PREFIX = "⚠️ Le modèle a terminé son tour sans réponse exploitable"
_INTERNAL_ERROR_TEXT = "⚠️ Erreur interne pendant la génération, réessayez."


def _is_approval_pending(content: str) -> bool:
    """Single entry point to recognize an approval pause, whether it's
    about a tool_call (require_approval, historical) or the whole PLAN
    (require_plan_approval, Iteration 3 — normal approval or escalation,
    two distinct prefixes). report_failure/reject_plan (FINAL messages,
    not pauses) match none of the three — treated as a final (failed)
    answer, no change needed."""
    return (
        content.startswith(_APPROVAL_PREFIX)
        or content.startswith(_PLAN_APPROVAL_PREFIX)
        or content.startswith(_PLAN_ESCALATION_PREFIX)
    )


def _docker_exec_python(container: str, script: str, timeout: int = 300) -> str:
    result = subprocess.run(
        ["docker", "exec", "-i", container, "python3", "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"docker exec dans {container} a échoué : {result.stderr}")
    return result.stdout


def _http_call(path: str, payload: dict, timeout: int) -> dict:
    body = json.dumps(payload)
    script = f"""
import json, urllib.request, urllib.error
req = urllib.request.Request(
    'http://localhost:8000{path}',
    data={body!r}.encode(),
    headers={{'Content-Type': 'application/json'}},
)
try:
    with urllib.request.urlopen(req, timeout={timeout}) as r:
        print(json.dumps({{"ok": True, "raw": r.read().decode()}}))
except urllib.error.HTTPError as e:
    print(json.dumps({{"ok": False, "error": e.read().decode()}}))
"""
    raw_out = _docker_exec_python(AGENT_CONTAINER, script, timeout=timeout + 20)
    result = json.loads(raw_out)
    if not result["ok"]:
        raise RuntimeError(f"appel {path} en échec : {result['error']}")
    return json.loads(result["raw"])


def _chat(prompt: str) -> str:
    data = _http_call(
        "/v1/chat/completions",
        {"model": "agent-llm", "messages": [{"role": "user", "content": prompt}], "stream": False},
        CHAT_TIMEOUT_SECONDS,
    )
    return data["choices"][0]["message"]["content"]


def _approve(prompt: str) -> str:
    data = _http_call(
        "/approve",
        {"messages": [{"role": "user", "content": prompt}], "approved": True, "grant_session": True},
        CHAT_TIMEOUT_SECONDS,
    )
    return data["content"]


def _parse_tool_calls(text: str) -> list:
    """
    [(name, args_dict), ...] from a text in the
    _format_approval_request/_format_iteration_limit_notice (app/main.py)
    format: `` `name`({...}) ``, several calls separated by ", ". The args
    are a Python dict repr (single quotes), not JSON — ast.literal_eval,
    not json.loads.
    """
    calls = []
    for m in re.finditer(r"`(\w+)`\((\{.*?\})\)", text):
        try:
            args = ast.literal_eval(m.group(2))
        except (ValueError, SyntaxError):
            args = {}
        calls.append((m.group(1), args))
    return calls


def _catalog_known_urls() -> set:
    urls = {f"{CATALOG_URL}/index.html"}
    urls |= {f"{CATALOG_URL}/page-{n}.html" for n in range(1, generate_catalog.N_PAGES + 1)}
    urls |= {f"{CATALOG_URL}/product-{i}.html" for i in range(1, generate_catalog.N_PRODUCTS + 1)}
    return urls


def _docs_known_urls() -> set:
    urls = {f"{DOCS_URL}/index.html", f"{DOCS_URL}/search.html", f"{DOCS_URL}/search-index.json"}
    urls |= {f"{DOCS_URL}/section-{n}.html" for n in range(1, generate_docs.N_FILLER_PAGES + 1)}
    urls.add(f"{DOCS_URL}/{generate_docs.INTERMEDIATE_PAGE}.html")
    urls.add(f"{DOCS_URL}/{generate_docs.TARGET_PAGE}.html")
    return urls


def _hr_app_known_urls() -> set:
    return {
        f"{HR_APP_URL}/",
        f"{HR_APP_URL}/login",
        f"{HR_APP_URL}/employees",
        f"{HR_APP_URL}/leave-form",
        f"{HR_APP_URL}/leave-form/submit",
        f"{HR_APP_URL}/leave-requests",
        f"{HR_APP_URL}/logout",
        f"{HR_APP_URL}/export/employees.csv",
        f"{HR_APP_URL}/health",
    }


# Task -> reference sitemap mapping for _classify_boucle_subcause.
# Absent from the dict (T8-T10, real sites) = no sub-classification possible.
KNOWN_URLS_BY_TASK = {
    "T1_extraction_paginee": _catalog_known_urls,
    "T7_impossible_par_construction": _catalog_known_urls,
    "T4_recherche_multi_sauts": _docs_known_urls,
    "T2_formulaire_conge": _hr_app_known_urls,
    "T3_tableau_dynamique": _hr_app_known_urls,
    "T5_telechargement_calcul": _hr_app_known_urls,
    "T6_session_authentifiee": _hr_app_known_urls,
}


def _derive_thread_id(prompt: str) -> str:
    """Same algorithm as _derive_thread_id (app/main.py): hashes only the
    1st human message, computable directly here (no need for a docker
    exec round trip just for a sha256 — found redundant, see
    test_tool_calling_baseline.py which already does this computation
    locally)."""
    return hashlib.sha256(prompt.encode()).hexdigest()[:16]


def _audit_entries(thread_id: str) -> list:
    script = f"""
import urllib.request
req = urllib.request.Request('http://localhost:8000/audit?thread_id={{}}'.format({thread_id!r}))
with urllib.request.urlopen(req, timeout=15) as r:
    print(r.read().decode())
"""
    raw = _docker_exec_python(AGENT_CONTAINER, script)
    return json.loads(raw).get("entries", [])


class TaskResult:
    def __init__(self):
        self.approvals = 0
        self.rounds = 0
        self.final_text = ""
        self.failure_cause = None  # None if dialogue succeeded (assertion checked separately)
        self.duration_seconds = 0.0
        self.error = None
        self.observed_navigate_urls = []
        # Best-effort proxy for the real number of tool_calls executed:
        # each "approvals" corresponds to a FIRST use of a tool in this
        # thread (the only case where a fresh approval is requested);
        # SUBSEQUENT uses of the same tool, auto-approved via the session
        # grant, never show up in the streamed text but ARE logged
        # (audit_log: auto-approved reversible tier traced — see
        # app/audit_log.py). tool_calls_observed = approvals + audit
        # entries for this thread. Doesn't claim to equal the exact
        # internal MAX_TOOL_ITERATIONS counter (not exposed by the API).
        self.tool_calls_observed = 0
        # Permanent observation-coverage judge (latency fix 1/2-ter, see
        # docs/history.md): verify_action now logs a role="verification"
        # entry on EVERY evaluation (usable or not, see app/audit_log.py).
        # Distinct from tool_calls_observed above: these entries have
        # kind="message", filtered separately so as not to inflate the
        # latter nor be confused with real tool_calls (kind absent, see
        # audit_log.log_tool_call).
        self.verification_opportunities = 0
        self.verification_exploitable = 0
        # "Total prefill per task" checkpoint judge (latency fix 2/2, see
        # docs/history.md): replaces the approximate cache=0 rate with its
        # real magnitude — the TIME actually spent processing prompt
        # tokens (cache hit or miss), read directly from TabbyAPI metrics
        # (`Process: N cached tokens and M new tokens at S T/s` -> M / S
        # prefill seconds per request, summed over THIS task's whole
        # real-time window). cache_zero_ratio stays recorded for
        # informational purposes (the old approximate rate).
        self.prefill_seconds = 0.0
        self.cache_zero_requests = 0
        self.tabbyapi_requests = 0
        # Real tokens/task judge (PLAN.md Phase 2, point 3 — see
        # campaign_persistence.aggregate_prefill_stats docstring for why
        # prefill_seconds alone conflates token volume with cache rate and
        # backend throughput): sum of cached_tokens+new_tokens across this
        # run's TabbyAPI calls, i.e. the real prompt size sent per call.
        self.prompt_tokens_total = 0
        # Campaign persistence (see campaign_persistence.py): thread_id
        # computed here with the same algorithm as _derive_thread_id
        # (app/main.py), join key with /workspace/.audit;
        # tabbyapi_raw_samples one sample PER REQUEST (not just the
        # aggregate above).
        self.thread_id = ""
        self.tabbyapi_raw_samples = []
        # Episode compaction coverage judge (PLAN.md Phase 2, point 2; see
        # app/graph.py, call_llm's role="episode_compaction" audit entry,
        # logged on EVERY call_llm regardless of EPISODE_COMPACTION_ENABLED):
        # episode_compaction_messages_max is this run's peak message count
        # (whether or not it ever crossed EPISODE_COMPACTION_TURN_THRESHOLD),
        # episode_compaction_applied_count is how many call_llm invocations
        # actually got compacted. A campaign where few runs approach the
        # threshold isn't a real measurement of the mechanism — see
        # docs/campaigns/2026-07-28_campaign_episode-compaction-enabled.md.
        self.episode_compaction_messages_max = 0
        self.episode_compaction_applied_count = 0


TABBYAPI_CONTAINER = os.environ.get("TABBYAPI_CONTAINER", "tabbyapi")


def run_task(prompt: str) -> TaskResult:
    result = TaskResult()
    start = time.monotonic()
    wall_start = datetime.now(timezone.utc)
    try:
        content = _chat(prompt)
        while _is_approval_pending(content):
            for name, args in _parse_tool_calls(content):
                if name == "browser_navigate" and "url" in args:
                    result.observed_navigate_urls.append(args["url"])
            result.approvals += 1
            result.rounds += 1
            if result.rounds > MAX_APPROVAL_ROUNDS:
                result.failure_cause = "boucle"
                result.final_text = content
                result.duration_seconds = time.monotonic() - start
                return result
            content = _approve(prompt)
        result.final_text = content
        if content.startswith(_ITERATION_LIMIT_PREFIX):
            result.failure_cause = "boucle"
            for name, args in _parse_tool_calls(content):
                if name == "browser_navigate" and "url" in args:
                    result.observed_navigate_urls.append(args["url"])
        elif content.startswith(_EMPTY_NOTICE_PREFIX):
            result.failure_cause = "extraction"
        elif _INTERNAL_ERROR_TEXT in content:
            result.failure_cause = "infra"
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        result.error = str(exc)
        result.failure_cause = "infra"
    result.duration_seconds = time.monotonic() - start
    result.thread_id = _derive_thread_id(prompt)

    try:
        entries = _audit_entries(result.thread_id)
    except (RuntimeError, subprocess.TimeoutExpired):
        entries = []
    # kind absent = real tool_call (log_tool_call); kind="message" =
    # assistant reasoning or observation-coverage entry (log_message, see
    # app/audit_log.py) — not to be mixed up.
    tool_call_entries = [e for e in entries if e.get("kind") is None]
    verification_entries = [e for e in entries if e.get("kind") == "message" and e.get("role") == "verification"]
    compaction_entries = [e for e in entries if e.get("kind") == "message" and e.get("role") == "episode_compaction"]
    result.tool_calls_observed = result.approvals + len(tool_call_entries)
    result.verification_opportunities = len(verification_entries)
    result.verification_exploitable = sum(
        1 for e in verification_entries if (e.get("content") or {}).get("exploitable")
    )
    if compaction_entries:
        result.episode_compaction_messages_max = max(
            (e.get("content") or {}).get("messages_count", 0) for e in compaction_entries
        )
    result.episode_compaction_applied_count = sum(
        1 for e in compaction_entries if (e.get("content") or {}).get("compacted")
    )
    for e in tool_call_entries:
        if e.get("tool") == "browser_navigate":
            url = e.get("arguments", {}).get("url")
            if url:
                result.observed_navigate_urls.append(url)

    wall_end = datetime.now(timezone.utc)
    result.tabbyapi_raw_samples = campaign_persistence.collect_tabbyapi_raw_samples(
        wall_start, wall_end, container=TABBYAPI_CONTAINER
    )
    prefill_stats = campaign_persistence.aggregate_prefill_stats(result.tabbyapi_raw_samples)
    result.prefill_seconds = prefill_stats["prefill_seconds"]
    result.cache_zero_requests = prefill_stats["cache_zero_requests"]
    result.tabbyapi_requests = prefill_stats["tabbyapi_requests"]
    result.prompt_tokens_total = prefill_stats["prompt_tokens_total"]
    return result


# ─────────────────────────────────────────────────────────────────────────
# Per-task assertions: (id, prompt, function(final_text, prompt) -> (bool, str))
# ─────────────────────────────────────────────────────────────────────────

_PRICE_RE = re.compile(r"84[.,]90")


def _assert_t1(text: str, _prompt: str):
    ok = bool(_PRICE_RE.search(text))
    return ok, "prix 84.90 trouvé" if ok else "prix 84.90 absent de la réponse"


def _assert_t2(text: str, _prompt: str):
    if not HR_APP_DATA_FILE.exists():
        return False, f"{HR_APP_DATA_FILE} absent : aucune soumission détectée"
    submissions = json.loads(HR_APP_DATA_FILE.read_text(encoding="utf-8"))
    if not submissions:
        return False, "fichier de soumissions vide"
    last = submissions[-1]
    expected = {
        "employee_name": "Marie Lefort",
        "start_date": "2026-08-03",
        "end_date": "2026-08-07",
        "reason": "conges_annuels",
        "urgent": True,
    }
    ok = last == expected
    return ok, f"dernière soumission = {last!r}" if not ok else "soumission conforme"


def _assert_t3(text: str, _prompt: str):
    ok = hr_data.T3_ANSWER_NAME in text
    return ok, f"attendu {hr_data.T3_ANSWER_NAME!r}" if not ok else "nom exact trouvé"


def _assert_t4(text: str, _prompt: str):
    has_value = generate_docs.TARGET_DEFAULT in text
    has_page = generate_docs.TARGET_PAGE in text
    ok = has_value and has_page
    return ok, f"valeur={has_value} page={has_page}"


def _assert_t5(text: str, _prompt: str):
    """
    Tolerates thousands separators (regular/non-breaking space, comma,
    period): observed under real conditions that the model naturally
    answers "199 000 €" rather than "199000" — a strict substring
    comparison wrongly classified this as a failure (see
    docs/campaigns/2026-07-22_diagnostic_budget60.md, T5 verification).
    """
    total = hr_data.T5_ANSWER_TOTAL
    pattern = re.escape(str(total // 1000)) + r"[\s,.  ]?" + f"{total % 1000:03d}"
    ok = bool(re.search(pattern, text))
    return ok, f"attendu {total}" if not ok else "masse salariale exacte trouvée"


def _assert_t6(text: str, _prompt: str):
    ok = str(hr_data.T6_ANSWER_PENDING_COUNT) in text
    return ok, f"attendu {hr_data.T6_ANSWER_PENDING_COUNT}" if not ok else "compte exact trouvé"


# "n'ai pas pu terminer"/"budget de tentatives": report_failure's phrasing
# (app/graph.py) — honest give-up after the replanning budget is
# exhausted, without ever having asserted a result. Distinct from an
# explicit absence declaration, but just as non-fabricated: counted as
# honest here so as not to penalize T7 when the failure path is the
# budget rather than an explicit conclusion (see docs/history.md, probe 4).
_ABSENCE_KEYWORDS = [
    "introuvable",
    "n'existe pas",
    "aucun produit",
    "pas trouvé",
    "inexistant",
    "n'ai pas pu terminer",
    "budget de tentatives",
]


def _assert_t7(text: str, _prompt: str):
    lowered = text.lower()
    declares_absence = any(k in lowered for k in _ABSENCE_KEYWORDS)
    has_price = bool(re.search(r"\d+[.,]\d{2}\s*€", text))
    ok = declares_absence and not has_price
    return ok, f"absence_declaree={declares_absence} prix_invente={has_price}"


def _assert_t8(text: str, _prompt: str):
    ok = "muret" in text.lower()
    return ok, "Muret absent de la réponse" if not ok else "Muret trouvé"


def _assert_t9(text: str, _prompt: str):
    ok = "insee" in text.lower()
    detail = "insee trouvé" if ok else "insee absent de la réponse (probable blocage externe, voir t9_blocked)"
    return ok, detail


def _assert_t10(text: str, _prompt: str):
    ok = "origin of species" in text.lower() and "10.01" in text
    return ok, "titre+prix attendus absents" if not ok else "titre+prix exacts trouvés"


def _fetch_latest_python_version() -> str:
    """Ground truth fetched LIVE on every campaign (see T11,
    docs/benchmark-v1.md): never frozen in the test, unlike the other
    local tasks.

    python.org returns gzip even with no explicit Accept-Encoding
    (observed by actually fetching it) — `urllib.request`, unlike
    `curl`/`requests`, never automatically decompresses based on
    Content-Encoding: manual decompression is mandatory, otherwise the
    regex never matches anything (silently, no error raised)."""
    import gzip

    with urllib.request.urlopen("https://www.python.org/downloads/", timeout=15) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    html = raw.decode("utf-8", errors="ignore")
    match = re.search(r"Download Python (3\.\d+\.\d+)", html)
    if not match:
        pytest.skip("impossible d'extraire la version Python courante depuis python.org")
    return match.group(1)


TASKS = [
    (
        "T1_extraction_paginee",
        f"Sur notre catalogue en ligne ({CATALOG_URL}/index.html), quel est "
        "le prix du produit référence KX-4471 ?",
        _assert_t1,
    ),
    (
        "T2_formulaire_conge",
        f"Sur {HR_APP_URL}/leave-form, remplis une demande de congé pour "
        "Marie Lefort, du 3 au 7 août 2026, motif \"congés annuels\", en la "
        "notant urgente.",
        _assert_t2,
    ),
    (
        "T3_tableau_dynamique",
        f"Sur {HR_APP_URL}/employees, dans la liste des employés, qui a le "
        "3e salaire le plus élevé du département Ingénierie ?",
        _assert_t3,
    ),
    (
        "T4_recherche_multi_sauts",
        f"Sur notre documentation ({DOCS_URL}/index.html), quelle est la "
        "valeur par défaut du paramètre max_retry_delay, et sur quelle page "
        "de la doc est-elle documentée ?",
        _assert_t4,
    ),
    (
        "T5_telechargement_calcul",
        f"Exporte le CSV des employés depuis {HR_APP_URL}/export/employees.csv "
        "et dis-moi la masse salariale totale du département Ventes.",
        _assert_t5,
    ),
    (
        "T6_session_authentifiee",
        f"Sur {HR_APP_URL}/login, connecte-toi avec l'identifiant "
        f"\"{hr_data.LOGIN_USERNAME}\" et le mot de passe "
        f"\"{hr_data.LOGIN_PASSWORD}\", puis dis-moi combien de demandes de "
        "congé sont en statut \"en attente\".",
        _assert_t6,
    ),
    (
        "T7_impossible_par_construction",
        f"Sur notre catalogue en ligne ({CATALOG_URL}/index.html), trouve la "
        "fiche du produit référence ZZ-9999 et donne-moi son prix.",
        _assert_t7,
    ),
    (
        "T8_wikipedia",
        "Sur Wikipédia en français, trouve dans quelle commune est né "
        "Clément Ader, puis, depuis l'article de cette commune, dans quel "
        "arrondissement elle se situe.",
        _assert_t8,
    ),
    (
        "T9_google_insee",
        "Via Google, trouve le site officiel de l'INSEE et donne-moi le "
        "titre exact de sa page d'accueil.",
        _assert_t9,
    ),
    (
        "T10_books_toscrape",
        "Sur books.toscrape.com, dans la catégorie Science, trouve le livre "
        "le moins cher encore en stock et donne son titre et son prix.",
        _assert_t10,
    ),
]


def _t11_task():
    version = _fetch_latest_python_version()
    prompt = "Quelle est la dernière version stable de Python ?"

    def _assert(text: str, _prompt: str):
        ok = version in text
        return ok, f"attendu {version}" if not ok else f"version {version} trouvée"

    return "T11_sonde_peremption", prompt, _assert


def _classify_boucle_subcause(task_id: str, result: TaskResult) -> str:
    """See the module docstring (finding #1): distinguishes a fabricated
    navigation (URL absent from the fixture's real sitemap) from a plain
    lack of iteration budget. No reference sitemap for T8-T10 (real
    sites): stays "boucle" as-is."""
    known_urls_fn = KNOWN_URLS_BY_TASK.get(task_id)
    if known_urls_fn is None:
        return "boucle"
    known = known_urls_fn()
    fabricated = [u for u in result.observed_navigate_urls if u not in known]
    return "boucle_fabrication" if fabricated else "boucle_budget"


def _classify_failure_cause(task_id: str, result: TaskResult, assertion_ok: bool, assertion_detail: str) -> str:
    if result.failure_cause == "boucle":
        return _classify_boucle_subcause(task_id, result)
    if result.failure_cause:
        return result.failure_cause
    if assertion_ok:
        return ""
    if task_id == "T9_google_insee":
        return "blocage_externe"
    if task_id == "T7_impossible_par_construction":
        return "hallucination"
    if task_id == "T11_sonde_peremption":
        return "hallucination"
    return "extraction"


HR_APP_CONTAINER = os.environ.get("HR_APP_CONTAINER", "fixture-hr-app")
# Volume shared between playwright-mcp (write) / filesystem-MCP
# (read-only) — see docker-compose.yml (agent-downloads) and
# docs/history.md "revised Phase 1d" (T5). Purged via playwright-mcp (the
# only side with write access to the volume).
PLAYWRIGHT_CONTAINER = os.environ.get("PLAYWRIGHT_CONTAINER", "playwright-mcp")


def _purge_downloads_volume() -> None:
    """
    Without this cleanup, a file downloaded by an earlier T5 repetition
    (even one that otherwise failed) would stay visible for the next
    repetition — which would then "succeed" by reading an artifact left
    by a previous run rather than actually downloading it itself, biasing
    the measured success rate (see docs/history.md, point 4 of the
    revised Phase 1d). Called before EVERY task repetition, not just at
    session setup: several tasks could one day trigger downloads, not
    just T5.
    """
    subprocess.run(
        ["docker", "exec", PLAYWRIGHT_CONTAINER, "sh", "-c", "rm -rf /downloads/* 2>/dev/null || true"],
        check=False,
    )


GHOSTDESK_CONTAINER = os.environ.get("GHOSTDESK_CONTAINER", "ghostdesk")


def _reset_ghostdesk_desktop() -> None:
    """
    Cross-task isolation, second channel (see docs/history.md, T9
    investigation): `app_launch` (GhostDesk) opens a REAL window on the
    `ghostdesk` container's desktop, at the MACHINE scale, with no
    relation whatsoever to the Playwright session already isolated by
    `_reset_browser_session` nor to the current langgraph-agent thread.
    Observed under real conditions: a Firefox launched by a T9 thread
    hours earlier stayed open on insee.fr; a later T9 thread, blocked by
    the anti-fabrication guardrail on browser_navigate, took a
    `screen_shot` and read this leftover Firefox — a "success" that
    proves nothing about the agent's ability to redo the task cold.
    `pkill -f firefox` (best-effort, check=False) before EVERY repetition,
    the same guarantee as the two resets already in place.
    """
    subprocess.run(
        ["docker", "exec", GHOSTDESK_CONTAINER, "pkill", "-f", "firefox"],
        check=False,
        capture_output=True,
    )


def _reset_browser_session() -> None:
    """
    Cross-task isolation (revised Phase 1d, see docs/history.md
    "cross-task isolation"): mcp-client's Playwright session is
    PERSISTENT and SHARED (see services/mcp-client/app/main.py,
    "browser"), not scoped per langgraph-agent thread or per task —
    without this reset, a tab left open by one task (e.g. T10,
    books.toscrape.com) stays visible in a COMPLETELY DIFFERENT
    subsequent task's snapshot (e.g. T7), sometimes several campaigns/
    hours later (observed under real conditions). Called before EVERY
    repetition, like `_purge_downloads_volume` — same guarantees, same
    scale. `check=False` (best-effort): a temporarily unavailable
    mcp-client must not fail the whole task for a simple preventive
    cleanup.
    """
    script = """
import urllib.request, urllib.error
req = urllib.request.Request('http://localhost:8003/reset-session/browser', data=b'', method='POST')
try:
    urllib.request.urlopen(req, timeout=10)
except urllib.error.HTTPError:
    pass
"""
    subprocess.run(
        ["docker", "exec", "-i", MCP_CLIENT_CONTAINER, "python3", "-c", script],
        check=False,
        capture_output=True,
    )


@pytest.fixture(scope="session", autouse=True)
def _reset_hr_submissions():
    """T2 checks the LAST submission: starting from a clean file avoids a
    previous campaign's submission masking a real failure.

    The file is written by the Flask container (root uid) on a bind
    mount: the pytest process (host, normal user uid) doesn't
    necessarily have permission to delete it directly (`PermissionError`
    observed under real conditions) — falls back to `docker exec` in the
    container that wrote it, which always has permission."""
    if HR_APP_DATA_FILE.exists():
        try:
            HR_APP_DATA_FILE.unlink()
        except PermissionError:
            subprocess.run(
                ["docker", "exec", HR_APP_CONTAINER, "rm", "-f", "/data/leave_submissions.json"],
                check=True,
            )
    yield


# B2 Part 2.1 (docs/briefs/B2-campaign-control.md): distinct exit code so
# run-campaign.sh (and anyone reading pytest's exit status) can tell "the
# campaign paused itself cleanly" apart from a genuine failure (1) or a
# clean finish (0) — pytest.exit() below sets this as the process exit code.
CAMPAIGN_PAUSED_EXIT_CODE = 75

# Staleness guard on resume (Part 3.5) — a warning, never a refusal (see
# campaign_persistence.check_resume_staleness).
CAMPAIGN_RESUME_STALENESS_DAYS = int(os.environ.get("CAMPAIGN_RESUME_STALENESS_DAYS", "7"))

# Resume mode (Part 2.3): set by run-campaign.sh --resume <cid>. Empty/unset
# means a fresh campaign — the overwhelmingly common case, unchanged.
RESUME_CAMPAIGN_ID = os.environ.get("WEB_TASKS_RESUME_CAMPAIGN_ID", "").strip() or None


def _build_task_plan():
    """Full task list (TASKS + T11), filtered by SMOKE_TASK_PREFIXES if
    set — factored out of _run_campaign() so a resume can rebuild the SAME
    id->(prompt, assert_fn) lookup (`tasks_by_id`) without needing the
    original smoke filter: a task_id already fixes its prompt/assertion
    regardless of which subset was launched originally."""
    tasks = list(TASKS)
    tasks.append(_t11_task())
    if not SMOKE_TASK_PREFIXES:
        return tasks
    # Bug found under real conditions (see docs/history.md): a plain
    # startswith(p) also matches "T1" against "T10_..."/"T11_..." (shared
    # numeric prefix) — requires the "_" boundary (or an exact match) to
    # match ONLY the intended task.
    filtered = [
        t for t in tasks
        if any(t[0] == p or t[0].startswith(p + "_") for p in SMOKE_TASK_PREFIXES)
    ]
    if not filtered:
        raise RuntimeError(
            f"WEB_TASKS_SMOKE_TASKS={SMOKE_TASK_PREFIXES!r} ne matche aucune tâche connue "
            f"(voir TASKS/_t11_task dans ce module)"
        )
    return filtered


def _run_campaign(resume_cid: str = None):
    # Campaign preamble (Iteration 0, docs/briefs/phase-1-coeur-cognitif.md):
    # raises PreflightError and stops BEFORE the first run if the tool
    # schema seen by langgraph-agent is stale/incomplete — see
    # campaign_preflight.py for the lesson motivating this guardrail. Run
    # identically on resume: a resume is exactly as exposed to a stale
    # tool schema/unreachable fixtures as a fresh launch.
    campaign_preflight.run_preflight(
        purge_downloads=_purge_downloads_volume,
        reset_browser_session=_reset_browser_session,
    )

    tasks_by_id = {t[0]: t for t in list(TASKS) + [_t11_task()]}

    metadata_now = campaign_persistence.collect_metadata(CAMPAIGN_LABEL)
    digest_now = campaign_persistence.config_digest(metadata_now)

    if resume_cid:
        # Part 2.3/3.3: reads the persisted state, refuses on config
        # drift (commit/image/flags different from what was recorded at
        # campaign START — never what a fresh collect_metadata() sees NOW
        # relabeled as "start"), warns (never refuses) on staleness, then
        # opens a new segment.
        cid = resume_cid
        progress_path = campaign_persistence.progress_json_path(CAMPAIGNS_DIR, cid)
        if not progress_path.exists():
            raise RuntimeError(f"reprise impossible : {progress_path} introuvable")
        state = campaign_persistence.read_campaign_json(progress_path)
        if not state.get("paused"):
            raise RuntimeError(f"campagne {cid} n'est pas en pause (paused=false dans {progress_path})")

        json_path = campaign_persistence.campaign_json_path(CAMPAIGNS_DIR, cid)
        campaign_data = campaign_persistence.read_campaign_json(json_path)
        rows = campaign_data["runs"]
        metadata = campaign_data["metadata"]
        started_at = metadata["started_at"]

        drift = campaign_persistence.config_drift_diff(metadata, metadata_now)
        if drift:
            raise campaign_preflight.PreflightError(
                f"reprise refusée : la configuration a dérivé depuis le lancement de {cid} ({drift})"
            )
        staleness_warning = campaign_persistence.check_resume_staleness(state, CAMPAIGN_RESUME_STALENESS_DAYS)
        if staleness_warning:
            print(f"AVERTISSEMENT : {staleness_warning}")

        segment_index = campaign_persistence.open_new_segment(state)
        state["paused"] = False
        campaign_persistence.write_progress_json(progress_path, state)
    else:
        tasks = _build_task_plan()
        metadata = metadata_now
        cid = campaign_persistence.campaign_id(CAMPAIGN_LABEL)
        started_at = datetime.now(timezone.utc).isoformat()
        progress_path = campaign_persistence.progress_json_path(CAMPAIGNS_DIR, cid)
        json_path = campaign_persistence.campaign_json_path(CAMPAIGNS_DIR, cid)
        planned = [
            {"task_id": task_id, "repetition": rep}
            for task_id, _, _ in tasks for rep in range(1, N_REPETITIONS + 1)
        ]
        state = campaign_persistence.init_progress_state(cid, CAMPAIGN_LABEL, started_at, digest_now, planned)
        campaign_persistence.write_progress_json(progress_path, state)
        rows = []
        segment_index = 0

    pause_path = campaign_persistence.pause_sentinel_path(CAMPAIGNS_DIR, cid)
    remaining = state["planned"][len(state["completed"]):]

    for entry in remaining:
        task_id, rep = entry["task_id"], entry["repetition"]
        base_prompt, assert_fn = tasks_by_id[task_id][1], tasks_by_id[task_id][2]

        # Unique marker per repetition (see _derive_thread_id, app/main.py:
        # hashes the EXACT text of the 1st human message) — same fix as
        # test_t7_noise_baseline/test_download_then_filesystem_read_roundtrip
        # below, never applied here before: without it, a task's
        # N_REPETITIONS share the SAME thread_id (fixed, identical
        # prompt), hence the SAME checkpointer state — a repetition that
        # blocks the thread before any checkpoint save (e.g. context
        # overflow) then makes the following repetitions replay on that
        # same blocked state, not independent attempts. Found on the
        # Iteration 4 final campaign (T8_wikipedia, see docs/history.md
        # and docs/resolved-bugs.md).
        prompt = f"{base_prompt} (essai {uuid.uuid4().hex[:8]})"
        # Computed before run_task() so the progress file can name the
        # in-flight thread_id for the dashboard to tail (B2 Part 1.2) —
        # same hash run_task()/result.thread_id ends up with.
        thread_id = _derive_thread_id(prompt)
        state["current"] = {
            "task_id": task_id,
            "repetition": rep,
            "thread_id": thread_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        campaign_persistence.write_progress_json(progress_path, state)

        _purge_downloads_volume()
        _reset_browser_session()
        _reset_ghostdesk_desktop()
        result = run_task(prompt)
        ok, detail = (False, result.error) if result.error else assert_fn(result.final_text, prompt)
        cause = _classify_failure_cause(task_id, result, ok, detail)
        fabricated_urls = [
            u for u in result.observed_navigate_urls
            if KNOWN_URLS_BY_TASK.get(task_id) and u not in KNOWN_URLS_BY_TASK[task_id]()
        ]
        row = {
            "task_id": task_id,
            "repetition": rep,
            "thread_id": result.thread_id,
            "success": ok,
            "detail": detail,
            "approvals": result.approvals,
            "tool_calls_observed": result.tool_calls_observed,
            "verification_opportunities": result.verification_opportunities,
            "verification_exploitable": result.verification_exploitable,
            "prefill_seconds": result.prefill_seconds,
            "cache_zero_requests": result.cache_zero_requests,
            "tabbyapi_requests": result.tabbyapi_requests,
            "prompt_tokens_total": result.prompt_tokens_total,
            "tabbyapi_raw_samples": result.tabbyapi_raw_samples,
            "fabricated_urls": fabricated_urls,
            "duration_seconds": round(result.duration_seconds, 1),
            "failure_cause": cause,
            "final_text": result.final_text,
            "episode_compaction_messages_max": result.episode_compaction_messages_max,
            "episode_compaction_applied_count": result.episode_compaction_applied_count,
            # B2 Part 3.1/3.2: needed by _write_report() to break down
            # cache-sensitive metrics per segment rather than pooling them
            # across a pause boundary (a fresh segment starts cold-cache
            # by construction after a tabbyapi restart).
            "segment": segment_index,
        }
        rows.append(row)
        campaign_persistence.append_campaign_row(json_path, metadata, started_at, row)

        state["completed"].append(
            {
                "task_id": task_id,
                "repetition": rep,
                "status": "success" if ok else "failure",
                "failure_cause": cause,
                "duration_s": row["duration_seconds"],
                "tool_calls": row["tool_calls_observed"],
                "thread_id": thread_id,
                # Extension beyond the brief's literal field list
                # (docs/briefs/B2-campaign-control.md, Part 1.1): Part
                # 1.3's running counters (CuP, fabrications, approvals)
                # need these per run — already computed for `row` above,
                # just also mirrored here instead of the dashboard
                # re-deriving them from nothing.
                "approvals": row["approvals"],
                "fabricated_urls_count": len(row["fabricated_urls"]),
                "segment": segment_index,
            }
        )
        state["current"] = None
        campaign_persistence.write_progress_json(progress_path, state)

        # Run-boundary pause check (Part 2.1): AFTER this run is fully
        # persisted (progress + full row), BEFORE the next one starts —
        # a run is atomic, pausing mid-run is out of scope (brief, Part
        # 2.1). The sentinel is consumed here so a resume doesn't
        # immediately re-trip on a leftover file.
        if pause_path.exists():
            pause_path.unlink(missing_ok=True)
            campaign_persistence.close_current_segment(state)
            state["paused"] = True
            campaign_persistence.write_progress_json(progress_path, state)
            _update_duration_stats(rows)
            pytest.exit(
                f"Campagne {cid} mise en pause après {len(state['completed'])}/{state['total_runs']} runs "
                f"(segment {segment_index})",
                returncode=CAMPAIGN_PAUSED_EXIT_CODE,
            )

    _update_duration_stats(rows)
    campaign_persistence.close_current_segment(state)
    campaign_persistence.write_progress_json(progress_path, state)
    return rows, cid, metadata, started_at


_ESTIMATE_CACHE_NOTE = (
    "Cache glissant d'ESTIMATION de durée, PAS un historique de campagnes : "
    "\"estimates\" est réécrit (médiane+plage fusionnées) à la fin de CHAQUE "
    "campagne, complète ou smoke — la valeur d'une tâche ne reflète donc que "
    "la DERNIÈRE campagne qui l'a mesurée, jamais une série dans le temps. "
    "Chaque entrée est {median, min, max, n} sur les n répétitions de CETTE "
    "campagne (B2 Part 1.4, docs/briefs/B2-campaign-control.md — la plage "
    "sert l'ETA, jamais un point unique). Sert à estimer la durée d'un "
    "prochain lancement avant de le démarrer (scripts/run-campaign.sh). Pour "
    "un historique par campagne (thread_id, métadonnées, métriques par "
    "run), voir campaign-<timestamp>-<label>.json (campaign_persistence.py)."
)


def _update_duration_stats(rows: list) -> None:
    """
    See ESTIMATE_CACHE_PATH above. Merges with already-persisted
    estimates (a task absent from THIS run, e.g. a targeted smoke, keeps
    its last known entry rather than being erased) — best-effort, never
    fails the campaign over a write issue with this side file
    (permissions, disk full...).
    """
    import statistics

    try:
        existing = json.loads(ESTIMATE_CACHE_PATH.read_text(encoding="utf-8")) if ESTIMATE_CACHE_PATH.exists() else {}
    except (OSError, ValueError):
        existing = {}
    estimates = existing.get("estimates", {})

    by_task = {}
    for r in rows:
        by_task.setdefault(r["task_id"], []).append(r["duration_seconds"])

    for task_id, durations in by_task.items():
        estimates[task_id] = {
            "median": round(statistics.median(durations), 1),
            "min": round(min(durations), 1),
            "max": round(max(durations), 1),
            "n": len(durations),
        }

    payload = {"_note": _ESTIMATE_CACHE_NOTE, "estimates": estimates}
    try:
        ESTIMATE_CACHE_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        pass


def _write_report(rows: list) -> None:
    """VIEW over the campaign JSON (see campaign_persistence.py and
    test_web_tasks_baseline below): `rows` comes from re-reading the
    freshly written `campaign-<timestamp>-<label>.json` file, never
    directly from `_run_campaign()`'s in-memory list — the JSON is the
    source of truth, this Markdown is just a rendering of it. Signature
    and rendering unchanged: the report stays visually identical to what
    it was before this effort."""
    by_task = {}
    for r in rows:
        by_task.setdefault(r["task_id"], []).append(r)

    # Episode compaction coverage judge (PLAN.md Phase 2, point 2): read
    # the REAL effective threshold from the running container (never
    # guessed/duplicated, CLAUDE.md #8) — best-effort, a docker failure
    # here must never break report generation.
    try:
        compaction_threshold = int(
            campaign_persistence.collect_env_flags(
                campaign_preflight.AGENT_CONTAINER, ["EPISODE_COMPACTION_TURN_THRESHOLD"]
            ).get("EPISODE_COMPACTION_TURN_THRESHOLD")
            or 0
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        compaction_threshold = None

    lines = [
        f"# {CAMPAIGN_LABEL} — suite de tâches web (Phase 0)",
        "",
        f"Générée automatiquement le {datetime.now(timezone.utc).isoformat()} "
        f"({N_REPETITIONS} répétitions/tâche). Voir docs/benchmark-v1.md pour la spec "
        "complète et les limites connues de chaque assertion, et la docstring "
        "de test_web_tasks.py pour la méthode de sous-classification "
        "boucle_fabrication/boucle_budget.",
        "",
        "| Tâche | Succès | Approbations (moy.) | Tool calls observés (moy.) | Couverture constats | Prefill total (s) | Cache=0 | Tokens prompt (total) | Durée (moy., s) | Messages max | Compactions | Causes d'échec |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    total_ok = 0
    total_n = 0
    total_opportunities = 0
    total_exploitable = 0
    total_prefill_seconds = 0.0
    total_cache_zero = 0
    total_tabbyapi_requests = 0
    total_prompt_tokens = 0
    total_threshold_crossed = 0
    total_compactions_applied = 0
    for task_id, task_rows in by_task.items():
        n_ok = sum(1 for r in task_rows if r["success"])
        n = len(task_rows)
        total_ok += n_ok
        total_n += n
        avg_approvals = sum(r["approvals"] for r in task_rows) / n
        avg_tool_calls = sum(r["tool_calls_observed"] for r in task_rows) / n
        avg_duration = sum(r["duration_seconds"] for r in task_rows) / n
        task_opportunities = sum(r["verification_opportunities"] for r in task_rows)
        task_exploitable = sum(r["verification_exploitable"] for r in task_rows)
        total_opportunities += task_opportunities
        total_exploitable += task_exploitable
        task_prefill = sum(r["prefill_seconds"] for r in task_rows)
        task_cache_zero = sum(r["cache_zero_requests"] for r in task_rows)
        task_tabbyapi_requests = sum(r["tabbyapi_requests"] for r in task_rows)
        task_prompt_tokens = sum(r["prompt_tokens_total"] for r in task_rows)
        total_prefill_seconds += task_prefill
        total_cache_zero += task_cache_zero
        total_tabbyapi_requests += task_tabbyapi_requests
        total_prompt_tokens += task_prompt_tokens
        coverage_str = (
            f"{100 * task_exploitable / task_opportunities:.0f}% ({task_exploitable}/{task_opportunities})"
            if task_opportunities
            else "—"
        )
        cache_zero_str = (
            f"{100 * task_cache_zero / task_tabbyapi_requests:.0f}% ({task_cache_zero}/{task_tabbyapi_requests})"
            if task_tabbyapi_requests
            else "—"
        )
        causes = Counter(r["failure_cause"] for r in task_rows if r["failure_cause"])
        causes_str = ", ".join(f"{c}×{n}" for c, n in causes.items()) or "—"
        task_messages_max = max((r["episode_compaction_messages_max"] for r in task_rows), default=0)
        task_threshold_crossed = sum(
            1 for r in task_rows
            if compaction_threshold and r["episode_compaction_messages_max"] > compaction_threshold
        )
        task_compactions_applied = sum(r["episode_compaction_applied_count"] for r in task_rows)
        total_threshold_crossed += task_threshold_crossed
        total_compactions_applied += task_compactions_applied
        lines.append(
            f"| {task_id} | {n_ok}/{n} | {avg_approvals:.1f} | {avg_tool_calls:.1f} | "
            f"{coverage_str} | {task_prefill:.1f} | {cache_zero_str} | {task_prompt_tokens} | {avg_duration:.1f} | "
            f"{task_messages_max} | {task_compactions_applied} | {causes_str} |"
        )

    lines.insert(3, f"**Score de campagne : {total_ok}/{total_n} passages réussis.**")
    # "Total prefill per task" checkpoint judge (latency fix 2/2, see
    # docs/history.md): replaces the cache=0 rate as the MAIN judge —
    # the latter stays recorded for informational purposes only (see the
    # "Cache=0" column above and this aggregated line).
    lines.insert(
        4,
        f"**Prefill total (toutes tâches) : {total_prefill_seconds:.1f}s** "
        f"({total_cache_zero}/{total_tabbyapi_requests} requêtes à cache=0, "
        f"{100*total_cache_zero/total_tabbyapi_requests:.1f}% — métrique informative)."
        if total_tabbyapi_requests else "",
    )
    # Permanent observation-coverage judge (latency fix 1/2-ter, see
    # docs/history.md, pass threshold >= 95%): usable observations /
    # total opportunities, all accumulated over the campaign — companion
    # to constats_inexploitables, which only measured ambiguity (not the
    # plain absence of an attempt).
    coverage_pct = 100 * total_exploitable / total_opportunities if total_opportunities else None
    coverage_line = (
        f"**Couverture des constats : {coverage_pct:.1f}% ({total_exploitable}/{total_opportunities}).**"
        if coverage_pct is not None
        else "**Couverture des constats : aucune opportunité observée (VERIFICATION_ENABLED désactivé ?).**"
    )
    lines.insert(4, coverage_line)
    # Real tokens/task judge (PLAN.md Phase 2, point 3 — see
    # campaign_persistence.aggregate_prefill_stats docstring): sum of
    # cached_tokens+new_tokens across ALL TabbyAPI calls in the campaign,
    # i.e. the real prompt volume sent — distinct from prefill_seconds
    # above, which conflates that volume with cache-hit rate and backend
    # throughput (found missing while requalifying the 2026-07-28 episode-
    # compaction campaign as "non concluant").
    if total_tabbyapi_requests:
        lines.insert(6, f"**Tokens de prompt (total, toutes tâches) : {total_prompt_tokens}.**")
    # Coverage judge for episode compaction (PLAN.md Phase 2, point 2, see
    # app/graph.py call_llm's role="episode_compaction" audit entry): a
    # campaign result only measures the mechanism if a meaningful share of
    # runs actually crossed EPISODE_COMPACTION_TURN_THRESHOLD — below that,
    # any score/token delta is noise, not evidence (see docs/campaigns/
    # 2026-07-28_campaign_episode-compaction-enabled.md, requalified
    # "non concluant" after this judge was added retroactively from
    # archives).
    if compaction_threshold is not None:
        crossed_pct = 100 * total_threshold_crossed / total_n if total_n else 0
        lines.insert(
            7 if total_tabbyapi_requests else 6,
            f"**Couverture compaction d'épisode : {total_threshold_crossed}/{total_n} runs "
            f"au-delà du seuil ({compaction_threshold} messages, {crossed_pct:.0f}%), "
            f"{total_compactions_applied} compaction(s) effectivement appliquée(s).**",
        )
    # Segment breakdown (B2 Part 3.1/3.2, docs/briefs/B2-campaign-control.md):
    # a paused-and-resumed campaign is NOT a continuous campaign — each
    # segment restarts tabbyapi, emptying the prefix cache, so pooling
    # prefill_seconds/cache_zero_rate/tokens-per-second ACROSS segments
    # (the "Prefill total" line above) would produce the same kind of
    # artefact as the invalid 14/33 campaign (docs/history.md). Score
    # metrics (CuP, success, failure causes) stay poolable (Part 3.4) —
    # only cache-sensitive figures are broken down here. A never-paused
    # campaign has exactly one segment: no breakdown needed, the pooled
    # line above already says everything.
    segment_ids = sorted({r.get("segment", 0) for r in rows})
    if len(segment_ids) > 1:
        lines.append("")
        lines.append("## Segments (pause/reprise — voir docs/briefs/archive/A6-campaign-control.md)")
        lines.append("")
        lines.append(
            "Métriques cache-sensibles (prefill, cache=0, tokens de prompt) jamais "
            "regroupées entre segments : chaque reprise redémarre tabbyapi, donc repart "
            "à cache froid. Les métriques de score (CuP, causes d'échec) restent "
            "cumulables — voir le tableau par tâche ci-dessus."
        )
        lines.append("")
        lines.append("| Segment | Runs | Succès | Prefill (s) | Cache=0 | Tokens prompt (total) |")
        lines.append("|---|---|---|---|---|---|")
        for seg in segment_ids:
            seg_rows = [r for r in rows if r.get("segment", 0) == seg]
            seg_ok = sum(1 for r in seg_rows if r["success"])
            seg_prefill = sum(r["prefill_seconds"] for r in seg_rows)
            seg_cache_zero = sum(r["cache_zero_requests"] for r in seg_rows)
            seg_requests = sum(r["tabbyapi_requests"] for r in seg_rows)
            seg_tokens = sum(r["prompt_tokens_total"] for r in seg_rows)
            cache_zero_str = f"{seg_cache_zero}/{seg_requests}" if seg_requests else "—"
            lines.append(
                f"| {seg} | {len(seg_rows)} | {seg_ok}/{len(seg_rows)} | {seg_prefill:.1f} | "
                f"{cache_zero_str} | {seg_tokens} |"
            )

    lines.append("")
    lines.append("## Détail par run")
    lines.append("")
    for r in rows:
        status = "✅" if r["success"] else "❌"
        fabricated_note = (
            f", URL fabriquées={r['fabricated_urls']}" if r["fabricated_urls"] else ""
        )
        coverage_note = (
            f", constats={r['verification_exploitable']}/{r['verification_opportunities']}"
            if r["verification_opportunities"]
            else ""
        )
        prefill_note = (
            f", prefill={r['prefill_seconds']:.1f}s, tokens_prompt={r['prompt_tokens_total']}"
            if r["tabbyapi_requests"]
            else ""
        )
        compaction_note = (
            f", messages_max={r['episode_compaction_messages_max']}"
            f"{', compactions=' + str(r['episode_compaction_applied_count']) if r['episode_compaction_applied_count'] else ''}"
            if r["episode_compaction_messages_max"]
            else ""
        )
        segment_note = f", segment={r.get('segment', 0)}" if len(segment_ids) > 1 else ""
        lines.append(
            f"- {status} `{r['task_id']}` #{r['repetition']} — {r['detail']} "
            f"(approbations={r['approvals']}, tool_calls_observés={r['tool_calls_observed']}, "
            f"durée={r['duration_seconds']}s"
            f"{', cause=' + r['failure_cause'] if r['failure_cause'] else ''}{fabricated_note}"
            f"{coverage_note}{prefill_note}{compaction_note}{segment_note})"
        )

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_web_tasks_baseline():
    # Context metadata (commit, image digests, loaded model, env flags) and
    # the campaign id are now collected INSIDE _run_campaign (B2 Part 1.1,
    # docs/briefs/B2-campaign-control.md) — the progress file needs the id
    # before the first run, not just after the last row is collected.
    # RESUME_CAMPAIGN_ID (Part 2.3): set by run-campaign.sh --resume, None
    # for the overwhelmingly common fresh-campaign case.
    rows, cid, metadata, started_at = _run_campaign(resume_cid=RESUME_CAMPAIGN_ID)
    ended_at = datetime.now(timezone.utc).isoformat()

    # _run_campaign() already wrote every row incrementally
    # (append_campaign_row, Part 1.1) — this final call is idempotent, it
    # only pins `ended_at` to the true campaign-completion instant. Same
    # directory _run_campaign() itself used (CAMPAIGNS_DIR, not
    # REPORT_PATH.parent — the two can differ under a custom
    # WEB_TASKS_REPORT_PATH, and the incremental writer needs one single
    # source of truth for where a resume will look).
    json_path = campaign_persistence.campaign_json_path(CAMPAIGNS_DIR, cid)
    campaign_persistence.write_campaign_json(json_path, metadata, started_at, ended_at, rows)

    # _write_report() is a VIEW: rendered from a RE-READ of the JSON just
    # written (not from `rows` directly) — guarantees the Markdown can
    # never diverge from what was actually persisted.
    persisted_rows = campaign_persistence.read_campaign_json(json_path)["runs"]
    _write_report(persisted_rows)

    # The harness itself must never fail silently: at least ONE task must
    # have run, even if the overall score is bad (that's exactly the
    # point-zero this test captures, not a quality assertion — see the
    # module docstring).
    assert rows, "aucune tâche exécutée"


T7_NOISE_REPORT_PATH = CAMPAIGNS_DIR / f"{datetime.now(timezone.utc):%Y-%m-%d}_diagnostic_t7-noise-live.md"


def test_t7_noise_baseline():
    """
    Dedicated noise measurement (revised Phase 1d, see docs/history.md
    "extraction fix"): T7 regresses 3/3 (1c) -> 1/3 (post-1d) with none of
    the identified variables (browser_evaluate, DOWNLOAD_DIRECTIVE,
    approval volume) explaining it in the archives — its 1c success
    already wasn't using browser_evaluate. With n=3, a 3/3->1/3 could be
    pure LLM variance (temperature=0.2, not 0). 5 additional repetitions
    HERE, at UNCHANGED CONFIGURATION (before the extraction fix), to size
    this noise BEFORE introducing a new variable — serves as a comparison
    baseline.
    """
    campaign_preflight.run_preflight(
        purge_downloads=_purge_downloads_volume,
        reset_browser_session=_reset_browser_session,
    )
    task_id, base_prompt, assert_fn = next(t for t in TASKS if t[0] == "T7_impossible_par_construction")
    rows = []
    for rep in range(1, 6):
        # Unique marker per repetition (see _derive_thread_id, app/main.py:
        # hashes the EXACT text of the 1st human message): without it,
        # the 5 "repetitions" would share the SAME thread as the previous
        # campaign (already warm, grants already given) — observed under
        # real conditions on a first attempt (0 approvals across the 5
        # repetitions, strictly identical detail and tool_calls_observed:
        # a sign the model was replaying from conversation memory, not an
        # independent measurement).
        prompt = f"{base_prompt} (essai {uuid.uuid4().hex[:8]})"
        _reset_browser_session()
        _reset_ghostdesk_desktop()
        result = run_task(prompt)
        ok, detail = (False, result.error) if result.error else assert_fn(result.final_text, prompt)
        rows.append(
            {
                "repetition": rep,
                "success": ok,
                "detail": detail,
                "approvals": result.approvals,
                "tool_calls_observed": result.tool_calls_observed,
                "duration_seconds": round(result.duration_seconds, 1),
            }
        )

    n_ok = sum(1 for r in rows if r["success"])
    lines = [
        "# T7 — mesure de bruit (5 répétitions, configuration post-1d inchangée)",
        "",
        f"Générée automatiquement le {datetime.now(timezone.utc).isoformat()}. "
        "Référence AVANT le correctif d'extraction (`browser_extract`) — voir docs/history.md.",
        "",
        f"**Score : {n_ok}/5.**",
        "",
        "| # | Succès | Détail | Approbations | Tool calls | Durée (s) |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        status = "✅" if r["success"] else "❌"
        lines.append(
            f"| {r['repetition']} | {status} | {r['detail']} | {r['approvals']} | "
            f"{r['tool_calls_observed']} | {r['duration_seconds']} |"
        )
    T7_NOISE_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    T7_NOISE_REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert rows, "aucune répétition exécutée"


def test_download_then_filesystem_read_roundtrip():
    """
    Dedicated test (revised Phase 1d, point 6 — see docs/history.md, T5):
    verifies the full download-volume round trip, isolated from the full
    campaign (repeated 3x, slower to diagnose on failure) — download
    triggered in the browser -> file actually present in the shared
    volume (verified directly via playwright-mcp, not just inferred from
    the agent's final answer) -> successful read via the filesystem tool
    -> assertion on the content (payroll total).

    `thread_id` derived by hashing the EXACT text of the first human
    message (see `app/main.py`, `_derive_thread_id`): without a unique
    marker per run, this test would reuse the SAME thread as an earlier
    run (including the full campaign) as long as the `langgraph-agent`
    container hasn't restarted — the agent would then correctly answer
    FROM MEMORY of the previous conversation, without re-downloading or
    re-reading the file, which would invalidate exactly the round-trip
    verification this test exists to do (observed under real conditions:
    an immediate replay after a first run answered in just 7s with not a
    single tool call).
    """
    campaign_preflight.run_preflight(
        purge_downloads=_purge_downloads_volume,
        reset_browser_session=_reset_browser_session,
    )
    task_id, prompt, assert_fn = next(t for t in TASKS if t[0] == "T5_telechargement_calcul")
    prompt = f"{prompt} (essai {uuid.uuid4().hex[:8]})"
    result = run_task(prompt)

    assert result.error is None, f"erreur infra : {result.error}"
    ok, detail = assert_fn(result.final_text, prompt)
    assert ok, detail

    listing = subprocess.run(
        ["docker", "exec", PLAYWRIGHT_CONTAINER, "sh", "-c", "ls /downloads/"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "employees.csv" in listing, (
        f"le fichier n'a pas atterri dans le volume partagé (contenu de /downloads/ : {listing!r}) "
        "— l'agent a peut-être trouvé un autre chemin pour répondre correctement plutôt que le "
        "round-trip download->filesystem attendu"
    )
