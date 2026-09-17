#!/usr/bin/env python3
"""
Full raw audit-log dump for one thread — every entry (any kind/role), not
just the adaptive_thinking/assistant pair read-adaptive-thinking-audit.py
filters for. Built to inspect the "boucle" (MAX_TOOL_ITERATIONS) failures
found in the qwen38-adaptive-thinking-campaign run: what tool calls the
agent actually made, in what order, to understand why it looped instead
of finishing (docs/engineering-log.md, "Qwen3.8-27B evaluation follow-up").

Same docker-exec-relay technique as read-adaptive-thinking-audit.py —
langgraph-agent publishes no host port.

Usage: python3 scripts/dump-audit-thread.py <thread_id>
"""
import json
import subprocess
import sys


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python3 scripts/dump-audit-thread.py <thread_id>", file=sys.stderr)
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
    print(f"{len(entries)} total entries for thread {thread_id}\n")

    for i, e in enumerate(entries):
        kind = e.get("kind")
        role = e.get("role")
        content = e.get("content")
        summary = ""
        if isinstance(content, dict):
            if "tool_calls" in content:
                calls = content["tool_calls"] or []
                summary = "; ".join(
                    f"{c.get('function', {}).get('name', c.get('name', '?'))}"
                    f"({c.get('function', {}).get('arguments', c.get('args', ''))})"
                    for c in calls
                )
            elif "content" in content:
                text = (content.get("content") or "")[:150].replace("\n", " ")
                summary = repr(text)
            elif "suppressed" in content:
                summary = f"suppressed={content['suppressed']}"
            else:
                summary = json.dumps(content, ensure_ascii=False)[:150]
        else:
            summary = repr(content)[:150]
        print(f"[{i}] kind={kind!s:12} role={role!s:12} {summary}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
