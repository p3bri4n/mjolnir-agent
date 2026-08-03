"""
Reference harness (Phase 0 of the langgraph/langchain-openai/openai
migration plan, see README): captures the current tool-calling behavior
against today's pinned trio (`langgraph==0.2.34`/`langchain-openai==0.2.2`/
`openai==1.51.2`) BEFORE any version bump, to be able to detect a
behavior regression afterward — not to fail CI on the model's own
quality.

5 fixed prompts (mix of simple / prior-reasoning / GhostDesk
capture->click / no tool / two tools), replayed `BASELINE_REPETITIONS`
times each (5 by default, model non-determinism accepted — see README,
several bugs already documented as non-deterministic). For each run,
best-effort classification of the outcome:

  - "structured"         : native tool_calls recognized by the server
                            (approval pause observed, OR auto-approved
                            execution traced in the audit log)
  - "fallback_recovered"  : `_extract_fallback_tool_call` had to catch a
                            call written in prose (detected via the
                            WARNING logged by `call_llm`, see app/graph.py)
  - "empty_notice"        : last-resort safety net, `_format_empty_answer_notice`
                            displayed (see app/main.py)
  - "ok_no_tool"          : normal text answer, no tool_calls signal

Known limitation of this classification (best-effort, no server
instrumentation beyond the existing audit log): a TIER_READ tool executed
alone, with no TIER_REVERSIBLE/TIER_SENSITIVE tool following in the same
turn, is neither logged (never audited, see approval_policy.py) nor
visible in the streamed text -> it would wrongly fall into "ok_no_tool".
The prompts below therefore deliberately target TIER_REVERSIBLE tools
(logged even when auto-approved) to stay detectable as a black box.

Additionally checks, on EVERY run, the SSE streaming's structural
invariants (independent of model quality, must hold even if the model
"drifts"):
  - OpenAI format (`chat.completion.chunk`, stream ended by `data:
    [DONE]`, last real chunk with `finish_reason: "stop"`);
  - at most one `<think>` tag across the whole streamed turn, opened at
    the very start of the turn (never after already-visible text) and
    closed exactly once if opened (see README, "merging a single
    continuous <think> block across several auto-approved tool-loop
    iterations").

At the end of the session, automatically writes/overwrites
`tests_integration/BASELINE.md` (summary table + per-run detail) — this
is the file to commit as the Phase 0 reference, and to replay as-is
after Phase 4 to compare before/after rates (see the plan).

Like `test_semantic_drift.py`: talks to real Docker containers via
`docker exec`, slow (real LLM generation) and non-deterministic by
nature. Skipped by default; explicit opt-in:

    RUN_LIVE_LLM_TESTS=1 python -m pytest tests_integration/test_tool_calling_baseline.py -v

Prerequisites identical to `test_semantic_drift.py`: `docker compose up`
with langgraph-agent/mcp-client/llama-server active.

⚠️ Stale (effort 1.2, docs/briefs/update-plan.md): PROMPTS below still
targets GhostDesk desktop tools (calculator launch, capture->click) —
those tools no longer exist in the schema (see docs/history.md). Kept
as the frozen historical Phase-0 baseline record rather than rewritten,
since changing the prompts would break BASELINE.md's comparability with
its own past runs; not runnable against the current stack without a
prompt redesign.

Deliberately slowed cadence between runs (see `_wait_for_llama_health`
and `BASELINE_PAUSE_SECONDS`): an early version of this harness, firing
the 25 real generations back-to-back with no pause, crashed
`llama-server` (observed under real conditions: `CUDA error: unspecified
launch failure` on a heterogeneous dual-GPU rig, `ggml-cuda.cu`) —
`llama-server` auto-restarts after this crash (`cmd_child_to_router`
supervisor), but requests falling during the model-reload window failed
in cascade (`httpcore.RemoteProtocolError: Server disconnected without
sending a response`), polluting the classification (a GPU crash isn't a
tool-calling failure). Waiting for `GET http://llama-server:8000/health`
in addition to a fixed pause before each run brings the cadence closer
to normal conversational use (never 25 back-to-back generations) rather
than a load test — this harness measures tool-calling, not
`llama-server`'s resilience under a burst, which remains an
infrastructure problem out of scope for this migration.
"""
import hashlib
import json
import os
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_LLM_TESTS") != "1",
    reason="live integration test (real LLM): opt-in via RUN_LIVE_LLM_TESTS=1, requires docker compose up",
)

AGENT_CONTAINER = os.environ.get("LANGGRAPH_AGENT_CONTAINER", "langgraph-agent")
N_REPETITIONS = int(os.environ.get("BASELINE_REPETITIONS", "5"))
# Fixed pause before each run, IN ADDITION to waiting for llama-server's
# health (see module docstring): brings the cadence closer to normal
# conversational use rather than a load test that crashed the GPU.
PAUSE_BETWEEN_RUNS_SECONDS = float(os.environ.get("BASELINE_PAUSE_SECONDS", "5"))
LLAMA_HEALTH_TIMEOUT_SECONDS = int(os.environ.get("BASELINE_HEALTH_TIMEOUT", "90"))

# Exact texts emitted server-side (see app/main.py): used to classify an
# outcome from the streamed text, without depending on the variable
# content that follows.
_APPROVAL_PREFIX = "⚠️ Approbation requise pour"
_ITERATION_LIMIT_PREFIX = "⚠️ Limite d'itérations d'outils atteinte"
_EMPTY_NOTICE_PREFIX = "⚠️ Le modèle a terminé son tour sans réponse exploitable"
_FALLBACK_LOG_MARKER = "Fallback tool call extracted"
# Text of _stream_response's `except Exception` fallback (app/main.py):
# an llama-server-side crash (e.g. CUDA, see module docstring) cuts the
# generation mid-way and this text then appears IN THE MIDDLE of the
# stream, not necessarily as a prefix — searched with `in`, not
# `startswith`, and checked first so as never to wrongly fall into
# "ok_no_tool".
_INTERNAL_ERROR_TEXT = "⚠️ Erreur interne pendant la génération, réessayez."

# (id, prompt, targeted tier) — the first 4 deliberately target a
# TIER_REVERSIBLE tool (auto-approved but logged, see docstring) to stay
# detectable as a black box; the 5th is deliberately out of MCP scope.
PROMPTS = [
    (
        "appel_simple",
        "Lance une calculatrice sur le bureau.",
    ),
    (
        "appel_apres_raisonnement",
        "Le bureau semble ne pas répondre. Réfléchis d'abord à la meilleure "
        "façon de vérifier ça sans rien casser, puis appuie sur la touche "
        "Échap pour voir si ça débloque quelque chose.",
    ),
    (
        "capture_puis_clic",
        "Prends une capture d'écran du bureau, repère un bouton ou une icône "
        "visible, puis clique dessus.",
    ),
    (
        "deux_outils",
        "Lance une application de calculatrice, puis une fois lancée, "
        "clique une fois au centre de l'écran pour lui donner le focus.",
    ),
    (
        "sans_outil",
        "Explique en une phrase la différence entre TCP et UDP.",
    ),
]


def _docker_exec_python(container: str, script: str, timeout: int = 260) -> str:
    result = subprocess.run(
        ["docker", "exec", "-i", container, "python3", "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"docker exec dans {container} a échoué : {result.stderr}")
    return result.stdout


def _wait_for_llama_health(timeout: int = LLAMA_HEALTH_TIMEOUT_SECONDS) -> None:
    """
    Waits for llama-server to answer healthy before launching a new run
    (see module docstring: a CUDA crash followed by an automatic restart
    leaves a window of several seconds where the model reloads, during
    which every request fails in cascade). Request made from the
    langgraph-agent container (internal compose network, `llama-server`
    service name resolved by Docker DNS) rather than from the host, which
    doesn't necessarily have the port published.
    """
    script = """
import urllib.request
try:
    with urllib.request.urlopen('http://llama-server:8000/health', timeout=5) as r:
        print(r.status)
except Exception as e:
    print(f"ERR:{e}")
"""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            last = _docker_exec_python(AGENT_CONTAINER, script, timeout=15).strip()
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            last = str(exc)
        if last == "200":
            return
        time.sleep(2)
    pytest.fail(
        f"llama-server ne répond pas sain après {timeout}s (dernier statut : {last!r}) "
        "— probable crash/redémarrage en cours, voir docker logs llama-server."
    )


    yield


def _log_line_count(container: str) -> int:
    result = subprocess.run(["docker", "logs", container], capture_output=True, text=True)
    return len((result.stdout + result.stderr).splitlines())


def _log_lines_since(container: str, since: int) -> str:
    result = subprocess.run(["docker", "logs", container], capture_output=True, text=True)
    lines = (result.stdout + result.stderr).splitlines()
    return "\n".join(lines[since:])


def _stream_chat(content: str) -> str:
    payload = json.dumps(
        {"model": "agent-llm", "messages": [{"role": "user", "content": content}], "stream": True}
    )
    script = f"""
import json, urllib.request, urllib.error
req = urllib.request.Request(
    'http://localhost:8000/v1/chat/completions',
    data={payload!r}.encode(),
    headers={{'Content-Type': 'application/json'}},
)
try:
    with urllib.request.urlopen(req, timeout=240) as r:
        raw = r.read().decode()
    print(json.dumps({{"ok": True, "raw": raw}}))
except urllib.error.HTTPError as e:
    print(json.dumps({{"ok": False, "error": e.read().decode()}}))
"""
    raw_out = _docker_exec_python(AGENT_CONTAINER, script)
    result = json.loads(raw_out)
    if not result["ok"]:
        pytest.fail(f"Requête streaming à langgraph-agent en échec : {result['error']}")
    return result["raw"]


def _get_audit_entries(thread_id: str) -> list:
    script = f"""
import urllib.request
req = urllib.request.Request('http://localhost:8000/audit?thread_id={thread_id}')
with urllib.request.urlopen(req, timeout=15) as r:
    print(r.read().decode())
"""
    raw = _docker_exec_python(AGENT_CONTAINER, script)
    return json.loads(raw).get("entries", [])


def _parse_sse(raw: str) -> list:
    chunks = []
    for line in raw.splitlines():
        if not line.startswith("data: "):
            continue
        data = line[len("data: ") :]
        chunks.append({"done": True} if data == "[DONE]" else json.loads(data))
    return chunks


def _assert_sse_invariants(raw: str, chunks: list) -> list:
    assert raw.rstrip("\n").endswith("data: [DONE]"), "le flux SSE doit se terminer par 'data: [DONE]'"
    assert chunks and chunks[-1] == {"done": True}, "marqueur [DONE] absent des chunks parsés"
    real_chunks = chunks[:-1]
    assert real_chunks, "aucun chunk de contenu reçu avant [DONE]"
    for chunk in real_chunks:
        assert chunk.get("object") == "chat.completion.chunk", f"chunk hors-format OpenAI : {chunk}"
        assert chunk.get("choices"), f"chunk sans 'choices' : {chunk}"
    assert real_chunks[-1]["choices"][0]["finish_reason"] == "stop", (
        f"dernier chunk réel sans finish_reason=stop : {real_chunks[-1]}"
    )
    return real_chunks


def _extract_full_text(real_chunks: list) -> str:
    parts = []
    for chunk in real_chunks:
        delta = chunk["choices"][0]["delta"]
        if delta.get("content"):
            parts.append(delta["content"])
    return "".join(parts)


def _assert_think_invariants(full_text: str) -> None:
    open_count = full_text.count("<think>")
    close_count = full_text.count("</think>")
    assert open_count <= 1, f"<think> ouvert {open_count} fois (attendu au plus 1) : {full_text[:300]}..."
    if open_count == 1:
        assert close_count == 1, f"<think> ouvert mais refermé {close_count} fois : {full_text[:300]}..."
        assert full_text.index("<think>") == 0, (
            f"<think> doit ouvrir le tour, avant tout texte visible : {full_text[:300]}..."
        )
        assert full_text.index("</think>") > full_text.index("<think>")


def _classify(full_text: str, fallback_logged: bool, audit_entries: list) -> str:
    if _INTERNAL_ERROR_TEXT in full_text:
        return "internal_error"
    visible = full_text.split("</think>", 1)[-1].strip() if "</think>" in full_text else full_text.strip()
    if visible.startswith(_EMPTY_NOTICE_PREFIX):
        return "empty_notice"
    if fallback_logged:
        return "fallback_recovered"
    if visible.startswith(_APPROVAL_PREFIX) or visible.startswith(_ITERATION_LIMIT_PREFIX):
        return "structured"
    if audit_entries:
        return "structured"
    return "ok_no_tool"


_RESULTS: list = []


@pytest.fixture(scope="session", autouse=True)
def _write_baseline_report():
    yield
    if not _RESULTS:
        return
    _write_baseline_md(_RESULTS)


def _write_baseline_md(results: list) -> None:
    path = os.path.join(os.path.dirname(__file__), "BASELINE.md")
    by_prompt = defaultdict(lambda: defaultdict(int))
    for r in results:
        by_prompt[r["prompt_id"]][r["classification"]] += 1

    categories = ["structured", "fallback_recovered", "empty_notice", "ok_no_tool", "internal_error"]
    lines = [
        "# Baseline tool-calling — trio actuel",
        "",
        "Trio de référence : `langgraph==0.2.34` / `langchain-openai==0.2.2` / "
        "`openai==1.51.2` (voir requirements.txt et README, section Streaming SSE).",
        "",
        f"Généré automatiquement par `test_tool_calling_baseline.py` le "
        f"{datetime.now(timezone.utc).isoformat()} — {N_REPETITIONS} répétitions par prompt "
        f"(pause {PAUSE_BETWEEN_RUNS_SECONDS}s + attente santé llama-server entre chaque run).",
        "",
        "| Prompt | structured | fallback_recovered | empty_notice | ok_no_tool | internal_error |",
        "|---|---|---|---|---|---|",
    ]
    for prompt_id, _ in PROMPTS:
        counts = by_prompt[prompt_id]
        lines.append(
            f"| `{prompt_id}` | " + " | ".join(str(counts.get(c, 0)) for c in categories) + " |"
        )
    lines += ["", "## Runs détaillés", ""]
    for r in results:
        lines.append(
            f"- `{r['prompt_id']}` rep{r['repetition']} -> **{r['classification']}** "
            f"({r['word_count']} mots, thread `{r['thread_id']}`)"
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


@pytest.mark.parametrize("repetition", range(N_REPETITIONS))
@pytest.mark.parametrize("prompt_id,prompt_text", PROMPTS, ids=[p[0] for p in PROMPTS])
def test_tool_calling_run(prompt_id, prompt_text, repetition):
    # Unique tag per run (id + repetition + pid) to derive a fresh
    # thread_id each time (_derive_thread_id, app/main.py, hash of the
    # first human message): without this, two runs of the same prompt
    # would share the same thread and the second would resume the
    # first's persisted state instead of starting a fresh task.
    tag = f"[baseline {prompt_id} rep{repetition} pid{os.getpid()}]"
    content = f"{tag} {prompt_text}"
    thread_id = hashlib.sha256(content.encode()).hexdigest()[:16]

    # Deliberately slowed cadence (see module docstring): waits for
    # llama-server to be healthy (covers a CUDA crash during the previous
    # run, still reloading the model) then marks a fixed pause, so as
    # never to fire two real generations back-to-back.
    _wait_for_llama_health()
    time.sleep(PAUSE_BETWEEN_RUNS_SECONDS)

    log_before = _log_line_count(AGENT_CONTAINER)
    raw = _stream_chat(content)
    new_logs = _log_lines_since(AGENT_CONTAINER, log_before)

    chunks = _parse_sse(raw)
    real_chunks = _assert_sse_invariants(raw, chunks)
    full_text = _extract_full_text(real_chunks)
    _assert_think_invariants(full_text)

    fallback_logged = _FALLBACK_LOG_MARKER in new_logs
    audit_entries = _get_audit_entries(thread_id)
    classification = _classify(full_text, fallback_logged, audit_entries)

    _RESULTS.append(
        {
            "prompt_id": prompt_id,
            "repetition": repetition,
            "classification": classification,
            "thread_id": thread_id,
            "word_count": len(full_text.split()),
        }
    )
