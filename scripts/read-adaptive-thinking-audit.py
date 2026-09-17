#!/usr/bin/env python3
"""
Phase 1's own judge, docs/briefs/qwen3.8-27b-evaluation.md: reads back a
thread's audit log (role="adaptive_thinking"/"assistant" entries) and
reports whether every turn where _should_suppress_thinking fired
produced no <think> block, and whether it fired at all (not a flattering
zero).

Runs the actual HTTP fetch via `docker exec` — langgraph-agent publishes
no host port (only reachable on agent-net from other containers) — a
plain host-side request would return nothing.

Usage: python3 scripts/read-adaptive-thinking-audit.py <thread_id> [--dump=N]
--dump=N prints the raw flag + assistant content for turn N (1-indexed)
instead of the pass/fail summary, for inspecting an individual verdict.
"""
import json
import subprocess
import sys


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print(
            "Usage: python3 scripts/read-adaptive-thinking-audit.py <thread_id> [--dump N]",
            file=sys.stderr,
        )
        return 1
    thread_id = sys.argv[1]
    dump_turn = None
    if len(sys.argv) == 3:
        if not sys.argv[2].startswith("--dump="):
            print("Third arg must be --dump=N (1-indexed turn)", file=sys.stderr)
            return 1
        dump_turn = int(sys.argv[2].split("=", 1)[1])

    fetch_code = (
        "import os, urllib.request\n"
        "url = f\"http://localhost:8000/audit?thread_id={os.environ['THREAD_ID']}\"\n"
        "print(urllib.request.urlopen(url, timeout=10).read().decode())\n"
    )
    result = subprocess.run(
        ["docker", "exec", "-e", f"THREAD_ID={thread_id}", "langgraph-agent", "python3", "-c", fetch_code],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"docker exec failed: {result.stderr}", file=sys.stderr)
        return 1

    data = json.loads(result.stdout)
    entries = data["entries"]

    thinking_flags = [e for e in entries if e.get("kind") == "message" and e.get("role") == "adaptive_thinking"]
    assistant_msgs = [e for e in entries if e.get("kind") == "message" and e.get("role") == "assistant"]

    print(f"{len(thinking_flags)} adaptive_thinking entries, {len(assistant_msgs)} assistant turns\n")

    if dump_turn is not None:
        idx = dump_turn - 1
        if not (0 <= idx < len(assistant_msgs)):
            print(f"Turn {dump_turn} out of range (1..{len(assistant_msgs)})", file=sys.stderr)
            return 1
        print(f"--- flag, turn {dump_turn} ---")
        print(json.dumps(thinking_flags[idx]["content"], indent=2, ensure_ascii=False))
        print(f"\n--- raw assistant content, turn {dump_turn} ---")
        print(msg_content := (assistant_msgs[idx]["content"].get("content") or ""))
        print(f"\n--- length: {len(msg_content)} chars ---")
        return 0

    hard_fail = False
    for i, (flag, msg) in enumerate(zip(thinking_flags, assistant_msgs)):
        suppressed = flag["content"]["suppressed"]
        content = msg["content"].get("content") or ""
        # Qwen3.8's chat_template.jinja bakes the OPENING <think> tag into
        # the generation prompt itself (both when thinking is on — only
        # "<think>\n" prefilled — and off — the full pre-closed
        # "<think>\n\n</think>\n\n" prefilled), never into what the model
        # generates. Only a real reasoning turn's own generated closing
        # </think> can appear in the completion; the opener never will,
        # unlike Qwen3.6 where our own reinjection wrapping (graph.py,
        # _convert_delta_with_reasoning) added both tags around a populated
        # reasoning_content field. Checking </think> generalizes across
        # both; checking <think> alone would be a structural blind spot on
        # Qwen3.8 (always false, suppressed or not).
        has_open_think = "<think>" in content
        think_pos = content.find("</think>")
        has_think = think_pos != -1
        # A suppressed turn that still shows </think> is only a REAL
        # suppression failure if there's substantial text before it (the
        # model actually reasoned). A live smoke found the alternative: the
        # model habitually re-closes an already-closed think block
        # (enable_thinking:false prefills the prompt with the full empty
        # "<think>\n\n</think>\n\n") as the very first thing it generates —
        # a near-zero-token cosmetic artifact, not a real reasoning burn.
        # Threshold is deliberately generous (whitespace/punctuation noise
        # only) — anything past it is real generated text, a real failure.
        leading_text = content[:think_pos].strip() if has_think else ""
        stray_marker_only = has_think and len(leading_text) <= 5
        if suppressed and has_think and not stray_marker_only:
            verdict = f"FAIL (suppressed but still reasoned, {len(leading_text)} leading chars)"
            hard_fail = True
        elif suppressed and stray_marker_only:
            verdict = "OK (stray </think> marker only, ~0 reasoning tokens)"
        elif not suppressed and not has_think:
            verdict = "WARN (not suppressed, but no </think> anyway)"
        else:
            verdict = "OK"
        print(
            f"turn {i + 1}: suppressed={suppressed!s:5} has_think={has_think!s:5} "
            f"has_open_think={has_open_think!s:5} [{verdict}]"
        )

    print()
    if not any(f["content"]["suppressed"] for f in thinking_flags):
        print("INCONCLUSIVE: suppressed never fired once in this run.")
        return 1
    if hard_fail:
        print("FAIL: at least one suppressed turn still produced a <think> block.")
        return 1
    print("PASS: every suppressed turn produced no <think> block.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
