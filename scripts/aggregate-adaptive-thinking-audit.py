#!/usr/bin/env python3
"""
Trigger-rate coverage counter for ADAPTIVE_THINKING across a full
campaign, not just one thread — CLAUDE.md's own rule: "a conditional
mechanism ships with its trigger-rate counter... never bolted on after a
campaign already came back unreadable." Loops every run's thread_id in a
campaign JSON, reads its audit log (same technique as
read-adaptive-thinking-audit.py), and tallies suppressed vs. eligible
turns, plus real suppression failures (substantial reasoning text before
a stray </think> — see read-adaptive-thinking-audit.py's own comment on
why a bare leading </think> is a harmless model habit, not a failure).

Usage: python3 scripts/aggregate-adaptive-thinking-audit.py <campaign.json>
"""
import json
import subprocess
import sys


def fetch_audit(thread_id: str) -> dict:
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
        raise RuntimeError(f"docker exec failed for {thread_id}: {result.stderr}")
    return json.loads(result.stdout)


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python3 scripts/aggregate-adaptive-thinking-audit.py <campaign.json>", file=sys.stderr)
        return 1

    campaign = json.load(open(sys.argv[1]))
    thread_ids = [r["thread_id"] for r in campaign["runs"] if r.get("thread_id")]
    print(f"{len(thread_ids)} runs with a thread_id (of {len(campaign['runs'])} total)\n")

    total_turns = 0
    total_suppressed = 0
    total_real_fails = 0
    threads_with_no_flags = 0

    for i, tid in enumerate(thread_ids, 1):
        try:
            data = fetch_audit(tid)
        except RuntimeError as e:
            print(f"[{i}/{len(thread_ids)}] {tid}: FETCH ERROR — {e}", file=sys.stderr)
            continue
        entries = data["entries"]
        flags = [e for e in entries if e.get("kind") == "message" and e.get("role") == "adaptive_thinking"]
        msgs = [e for e in entries if e.get("kind") == "message" and e.get("role") == "assistant"]
        if not flags:
            threads_with_no_flags += 1
            continue
        for flag, msg in zip(flags, msgs):
            total_turns += 1
            suppressed = flag["content"]["suppressed"]
            if suppressed:
                total_suppressed += 1
            content = msg["content"].get("content") or ""
            think_pos = content.find("</think>")
            if think_pos != -1:
                leading = content[:think_pos].strip()
                if suppressed and len(leading) > 5:
                    total_real_fails += 1
                    print(f"[{i}/{len(thread_ids)}] {tid}: REAL suppression failure, {len(leading)} leading chars")

    print()
    print(f"Threads with zero adaptive_thinking entries: {threads_with_no_flags}")
    print(f"Total eligible turns: {total_turns}")
    if total_turns:
        print(f"Suppressed: {total_suppressed} ({100 * total_suppressed / total_turns:.1f}%)")
    print(f"Real suppression failures (substantial reasoning despite suppression): {total_real_fails}")
    if total_turns and total_suppressed == 0:
        print("\nFLATTERING ZERO: ADAPTIVE_THINKING never suppressed a single turn in this campaign.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
