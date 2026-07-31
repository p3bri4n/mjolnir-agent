"""
Targeted compaction exercise — MULTI-TURN THREADS, deliberately OUTSIDE
the frozen benchmark (docs/benchmark-v1.md/v2.md, test_web_tasks*.py):
never added to the official suite, same discipline as the abandoned
tests_integration/probe_episode_compaction.py (imported here for
reference, never modified).

Why this exists instead of one long task (see docs/benchmark-v2.md,
"Famille A4 / compaction — clôture"): `tool_iterations` (app/graph.py)
only resets on a new top-level user message, never on a replan — a
budget CUMULATIVE for one task's whole lifetime. At ~2 messages per
tool_call<->result cycle, MAX_TOOL_ITERATIONS=20 arithmetically caps a
SINGLE task at ~40-42 messages — exactly the 41 observed on family A4,
and the same reason the reverted 9-step A4 extension failed 0/3
(MAX_TOOL_ITERATIONS reached before completion). A single task
guaranteeing >60 messages is therefore not achievable without loosening
that frozen, measured budget — not done here, not as a side effect of
building a validation exercise (CLAUDE.md, measured-behavior section).

Instead: several ordinary top-level user turns in the SAME thread (each
turn resets tool_iterations independently, so each stays comfortably
under MAX_TOOL_ITERATIONS on its own), while the thread's accumulated
message history — what episode_compaction actually counts and acts on —
keeps growing across turns. Closer to the real usage pattern the
mechanism exists for (a thread that runs long) than an artificially
long single task.

Each thread's turns are built from EXISTING, frozen v1 task prompts/
ground truths (T1/T3/T4/T5/T6 — imported from test_web_tasks.py, never
modified), recombined in a new order — no new fixture content. The
FIRST turn also states one fact ONLY in the chat message itself, never
written on any page: this is the one thing episode_compaction could
plausibly destroy (`_summarize_subtask`, app/graph.py, keeps only the
subtask description + tool_call arguments + verify_action's generic
verdict string — never a ToolMessage's actual content). The LAST turn
requires recalling that fact. A page-derivable fact (e.g. the KX-4471
price, re-fetchable by navigating back) would not test this: the agent
could just look it up again, masking a summary that silently dropped it.

Exercise validity, not mechanism validity (per the checkpoint decision):
if a thread's final message_count (read via POST /context, the same
source the observability dashboard uses) doesn't exceed
COMPACTION_EXERCISE_THRESHOLD, the run is marked invalid and excluded
from the coverage judge — the EXERCISE failed to reach its own design
target, not the compaction mechanism.

MAX_TOOL_ITERATIONS is NOT touched anywhere in this file.

Usage:
    python3 probe_compaction_multi_turn.py --flag-label off|on [--reps N]
"""

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # pour "tests_integration" en package

from tests_integration import campaign_persistence  # noqa: E402
from tests_integration.test_web_tasks import (  # noqa: E402
    CATALOG_URL,
    CHAT_TIMEOUT_SECONDS,
    DOCS_URL,
    HR_APP_URL,
    MAX_APPROVAL_ROUNDS,
    TABBYAPI_CONTAINER,
    _EMPTY_NOTICE_PREFIX,
    _INTERNAL_ERROR_TEXT,
    _ITERATION_LIMIT_PREFIX,
    _audit_entries,
    _derive_thread_id,
    _http_call,
    _is_approval_pending,
    _purge_downloads_volume,
    _reset_browser_session,
    _reset_ghostdesk_desktop,
    hr_data,
)

COMPACTION_EXERCISE_THRESHOLD = 60


def _chat_multi(messages: list) -> str:
    """Like test_web_tasks._chat, but sends the FULL history built so
    far (this exercise's own turns) rather than a single prompt — same
    contract Open WebUI uses in production (app/main.py's own docstring:
    "resends the full history on every request"). thread_id stays the
    hash of messages[0]["content"] across every call, so all turns land
    on the same LangGraph thread."""
    data = _http_call(
        "/v1/chat/completions",
        {"model": "agent-llm", "messages": messages, "stream": False},
        CHAT_TIMEOUT_SECONDS,
    )
    return data["choices"][0]["message"]["content"]


def _approve_multi(messages: list) -> str:
    data = _http_call(
        "/approve",
        {"messages": messages, "approved": True, "grant_session": True},
        CHAT_TIMEOUT_SECONDS,
    )
    return data["content"]


def _context_message_count(thread_id: str) -> int:
    """POST /context (app/main.py) with an explicit thread_id — the same
    endpoint the observability dashboard polls, reused here as the
    exercise's own validity gate rather than reconstructing message
    counts by hand."""
    data = _http_call("/context", {"thread_id": thread_id}, CHAT_TIMEOUT_SECONDS)
    return data["message_count"]


class TurnResult:
    def __init__(self, label: str):
        self.label = label
        self.final_text = ""
        self.approvals = 0
        self.rounds = 0
        self.failure_cause = None
        self.error = None


def _run_turn(history: list, label: str, prompt: str) -> TurnResult:
    """Runs one top-level user turn against the FULL history accumulated
    so far, mutating `history` in place with the new user message and
    the resolved assistant answer — same approval-pending loop as
    test_web_tasks.run_task, generalized to a message list instead of a
    single prompt."""
    result = TurnResult(label)
    history.append({"role": "user", "content": prompt})
    try:
        content = _chat_multi(history)
        while _is_approval_pending(content):
            result.approvals += 1
            result.rounds += 1
            if result.rounds > MAX_APPROVAL_ROUNDS:
                result.failure_cause = "boucle"
                result.final_text = content
                return result
            content = _approve_multi(history)
        result.final_text = content
        if content.startswith(_ITERATION_LIMIT_PREFIX):
            # This turn alone hit MAX_TOOL_ITERATIONS — the per-turn
            # budget guarantee this exercise relies on (each turn
            # "confortablement sous MAX_TOOL_ITERATIONS") was violated;
            # a design problem with THIS turn, not a compaction result.
            result.failure_cause = "boucle"
        elif content.startswith(_EMPTY_NOTICE_PREFIX):
            result.failure_cause = "extraction"
        elif _INTERNAL_ERROR_TEXT in content:
            result.failure_cause = "infra"
    except Exception as exc:  # noqa: BLE001 - surfaced via result.error, never crashes the exercise
        result.error = str(exc)
        result.failure_cause = "infra"
    history.append({"role": "assistant", "content": result.final_text})
    return result


# ─────────────────────────────────────────────────────────────────────────
# Thread definitions: each is (thread_label, [(turn_label, prompt), ...],
# dependent_check(final_text) -> (bool, str)). The LAST turn is always
# the dependent one. Turn prompts reuse T1/T3/T4/T5/T6's EXACT wording
# and ground truths (test_web_tasks.py, TASKS) — imported implicitly via
# the same URLs/fixture modules, never redefined by hand.
# ─────────────────────────────────────────────────────────────────────────

_T1_PROMPT = f"Sur notre catalogue en ligne ({CATALOG_URL}/index.html), quel est le prix du produit référence KX-4471 ?"
_T3_PROMPT = f"Sur {HR_APP_URL}/employees, dans la liste des employés, qui a le 3e salaire le plus élevé du département Ingénierie ?"
_T4_PROMPT = (
    f"Sur notre documentation ({DOCS_URL}/index.html), quelle est la valeur "
    "par défaut du paramètre max_retry_delay, et sur quelle page de la doc "
    "est-elle documentée ?"
)
_T5_PROMPT = (
    f"Exporte le CSV des employés depuis {HR_APP_URL}/export/employees.csv "
    "et dis-moi la masse salariale totale du département Ventes."
)
_T6_PROMPT = (
    f"Sur {HR_APP_URL}/login, connecte-toi avec l'identifiant "
    f'"{hr_data.LOGIN_USERNAME}" et le mot de passe "{hr_data.LOGIN_PASSWORD}", '
    'puis dis-moi combien de demandes de congé sont en statut "en attente".'
)

_BUDGET_VALUE = "180"


def _thread_budget_kx4471(marker: str) -> tuple:
    turns = [
        (
            "T1_plus_budget",
            f"Mon budget interne pour cet achat est de {_BUDGET_VALUE} euros — "
            "retiens-le, je te le redemanderai à la fin de notre échange. Pour "
            f"l'instant : {_T1_PROMPT} (essai {marker})",
        ),
        ("T4_filler", _T4_PROMPT),
        ("T3_filler", _T3_PROMPT),
        ("T5_filler", _T5_PROMPT),
        ("T6_filler", _T6_PROMPT),
        (
            "dependent_recall",
            "Pour finir : rappelle-moi le prix du produit KX-4471 qu'on a "
            "trouvé au tout début de notre échange, ainsi que mon budget "
            "interne que je t'avais donné à ce moment-là, et dis-moi si "
            "l'achat est dans les clous (prix inférieur ou égal au budget).",
        ),
    ]

    def dependent_check(text: str) -> tuple:
        has_budget = _BUDGET_VALUE in text
        has_price = "84,90" in text or "84.90" in text
        ok = has_budget and has_price
        return ok, f"budget={has_budget} prix={has_price}"

    return turns, dependent_check


_CODE_VALUE = "ROUGE-12"


def _thread_code_interne(marker: str) -> tuple:
    turns = [
        (
            "T6_plus_code",
            f"Le nom de code de cette session est {_CODE_VALUE} — garde-le en "
            f"tête, je te le redemanderai en tout dernier tour. {_T6_PROMPT} "
            f"(essai {marker})",
        ),
        ("T5_filler", _T5_PROMPT),
        ("T1_filler", _T1_PROMPT),
        ("T4_filler", _T4_PROMPT),
        ("T3_filler", _T3_PROMPT),
        (
            "dependent_recall",
            "Pour finir, sans naviguer nulle part : quel est le nom de code "
            "que je t'ai donné au tout premier tour de notre échange ? "
            "Redonne-le-moi exactement.",
        ),
    ]

    def dependent_check(text: str) -> tuple:
        ok = _CODE_VALUE in text
        return ok, f"code {_CODE_VALUE!r} {'trouvé' if ok else 'absent de la réponse'}"

    return turns, dependent_check


THREADS = {
    "budget_kx4471": _thread_budget_kx4471,
    "code_interne": _thread_code_interne,
}


def run_one_thread(thread_name: str, rep: int, flag_label: str) -> dict:
    marker = f"{flag_label}-{uuid.uuid4().hex[:8]}"
    turns_spec, dependent_check = THREADS[thread_name](marker)

    _purge_downloads_volume()
    _reset_browser_session()
    _reset_ghostdesk_desktop()

    wall_start = datetime.now(timezone.utc)
    history: list = []
    turn_results = []
    for label, prompt in turns_spec:
        turn_results.append(_run_turn(history, label, prompt))
    wall_end = datetime.now(timezone.utc)

    first_turn_prompt = turns_spec[0][1]
    thread_id = _derive_thread_id(first_turn_prompt)

    try:
        message_count = _context_message_count(thread_id)
    except Exception:
        message_count = 0
    exercise_valid = message_count > COMPACTION_EXERCISE_THRESHOLD

    last_turn = turn_results[-1]
    if last_turn.error or last_turn.failure_cause:
        dependent_success, dependent_detail = False, last_turn.failure_cause or last_turn.error
    else:
        dependent_success, dependent_detail = dependent_check(last_turn.final_text)

    any_turn_hit_iteration_limit = any(t.failure_cause == "boucle" for t in turn_results)

    try:
        entries = _audit_entries(thread_id)
    except Exception:
        entries = []
    compaction_entries = [e for e in entries if e.get("kind") == "message" and e.get("role") == "episode_compaction"]
    messages_max = (
        max((e.get("content") or {}).get("messages_count", 0) for e in compaction_entries) if compaction_entries else 0
    )
    compactions_applied = sum(1 for e in compaction_entries if (e.get("content") or {}).get("compacted"))
    tool_call_entries = [e for e in entries if e.get("kind") is None]

    samples = campaign_persistence.collect_tabbyapi_raw_samples(wall_start, wall_end, container=TABBYAPI_CONTAINER)
    prefill_stats = campaign_persistence.aggregate_prefill_stats(samples)

    row = {
        "thread": thread_name,
        "flag": flag_label,
        "rep": rep,
        "exercise_valid": exercise_valid,
        "message_count": message_count,
        "dependent_success": dependent_success,
        "dependent_detail": dependent_detail,
        "any_turn_hit_iteration_limit": any_turn_hit_iteration_limit,
        "messages_max": messages_max,
        "compactions_applied": compactions_applied,
        "tool_calls_observed": len(tool_call_entries),
        "prefill_seconds": prefill_stats["prefill_seconds"],
        "prompt_tokens_total": prefill_stats["prompt_tokens_total"],
        "cache_zero_requests": prefill_stats["cache_zero_requests"],
        "tabbyapi_requests": prefill_stats["tabbyapi_requests"],
        "turns": [
            {
                "label": t.label,
                "failure_cause": t.failure_cause,
                "approvals": t.approvals,
                "final_text": t.final_text,
            }
            for t in turn_results
        ],
    }
    print(
        f"[{flag_label}] {thread_name} rep {rep}: valid={exercise_valid} "
        f"message_count={message_count} dependent_success={dependent_success} "
        f"({dependent_detail}) compactions={compactions_applied} "
        f"tool_calls={row['tool_calls_observed']} tokens_prompt={row['prompt_tokens_total']}",
        flush=True,
    )
    if any_turn_hit_iteration_limit:
        print("    -> AU MOINS UN TOUR A ATTEINT MAX_TOOL_ITERATIONS (design du tour à revoir)", flush=True)
    return row


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--flag-label", required=True, choices=["off", "on"])
    parser.add_argument("--threads", default=",".join(THREADS), help="Sous-ensemble de fils, séparés par des virgules.")
    args = parser.parse_args()

    thread_names = [t.strip() for t in args.threads.split(",") if t.strip()]
    rows = []
    for thread_name in thread_names:
        for rep in range(1, args.reps + 1):
            rows.append(run_one_thread(thread_name, rep, args.flag_label))

    out_path = Path(__file__).parent / f"probe_compaction_multi_turn_{args.flag_label}.json"
    out_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"--- écrit {out_path} ---", flush=True)

    invalid = [r for r in rows if not r["exercise_valid"]]
    if invalid:
        print(
            f"⚠️ {len(invalid)}/{len(rows)} run(s) sous le seuil de {COMPACTION_EXERCISE_THRESHOLD} messages "
            "— EXERCICE invalide pour ces runs (pas le mécanisme) : à revoir avant toute conclusion.",
            flush=True,
        )


if __name__ == "__main__":
    main()
