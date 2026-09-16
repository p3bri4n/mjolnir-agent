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

Usage: python3 scripts/read-adaptive-thinking-audit.py <thread_id>
"""
import json
import subprocess
import sys


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python3 scripts/read-adaptive-thinking-audit.py <thread_id>", file=sys.stderr)
        return 1
    thread_id = sys.argv[1]

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

    hard_fail = False
    for i, (flag, msg) in enumerate(zip(thinking_flags, assistant_msgs)):
        suppressed = flag["content"]["suppressed"]
        content = msg["content"].get("content") or ""
        has_think = "<think>" in content
        if suppressed and has_think:
            verdict = "FAIL (suppressed but still reasoned)"
            hard_fail = True
        elif not suppressed and not has_think:
            verdict = "WARN (not suppressed, but no <think> anyway)"
        else:
            verdict = "OK"
        print(f"turn {i + 1}: suppressed={suppressed!s:5} has_think={has_think!s:5} [{verdict}]")

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
