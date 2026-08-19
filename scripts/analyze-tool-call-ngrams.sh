#!/usr/bin/env bash
# Frequency analysis of tool-call action sequences in the real audit log
# (docs/briefs/scaffolding-optimisation.md, Effort 3, point 3.1: "find the
# candidates in the archives, not by intuition" — before any composite
# tool is designed). Zero agent calls: reads the JSONL audit trail
# directly from the host-mounted volume (docker-compose.yml's
# ./workspace:/workspace), no docker/GPU needed.
#
# Only REAL EXECUTED tool_calls are counted (entries carrying a "tool"
# key with no "kind" key — see app/audit_log.py's kind convention,
# already documented in test_web_tasks.py: "kind absent = real tool_call
# (log_tool_call); kind='message' = assistant reasoning or an
# observation-coverage entry"). Model-proposed-but-not-yet-approved tool
# calls (kind="message", role="assistant") are deliberately excluded —
# the brief asks where turns actually GO, not what the model merely
# proposed.
#
# For each thread (chronological order within the thread, never crossing
# thread boundaries), computes every contiguous tool-name n-gram for
# n=2..5, ranks by "turns that would be saved" if the n-gram collapsed
# into one composite call: (n-1) * observed_count.
#
# Usage: bash scripts/analyze-tool-call-ngrams.sh [audit_dir] [top_n]
#   audit_dir defaults to workspace/.audit (relative to the repo root)
#   top_n defaults to 20 (candidates printed per n-gram length)

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

AUDIT_DIR="${1:-workspace/.audit}"
TOP_N="${2:-20}"

if [ ! -d "$AUDIT_DIR" ]; then
  echo "Audit directory not found: $AUDIT_DIR" >&2
  echo "Expected the host-mounted volume from docker-compose.yml (./workspace:/workspace)." >&2
  exit 1
fi

python3 - "$AUDIT_DIR" "$TOP_N" <<'PYEOF'
import glob
import json
import sys
from collections import Counter, defaultdict

audit_dir, top_n = sys.argv[1], int(sys.argv[2])

# thread_id -> list of (timestamp, tool_name), filled in file order then
# sorted per-thread (concurrent campaigns, effort 1.3, can interleave
# threads across a single day's file).
by_thread = defaultdict(list)
total_lines = 0
total_tool_calls = 0
tool_frequency = Counter()

for path in sorted(glob.glob(f"{audit_dir}/*.jsonl")):
    with open(path, encoding="utf-8") as f:
        for line in f:
            total_lines += 1
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "tool" not in entry or "kind" in entry:
                continue
            total_tool_calls += 1
            thread_id = entry.get("thread_id", "")
            tool_frequency[entry["tool"]] += 1
            by_thread[thread_id].append((entry.get("timestamp", ""), entry["tool"]))

sequences = []
for thread_id, calls in by_thread.items():
    calls.sort(key=lambda c: c[0])
    sequences.append([tool for _, tool in calls])

print(f"Audit dir: {audit_dir}")
print(f"Total lines scanned: {total_lines}")
print(f"Real tool_calls found: {total_tool_calls}")
print(f"Distinct threads with >=1 tool_call: {len(sequences)}")
print()
print("=== Overall tool frequency (single calls, for context) ===")
for tool, count in tool_frequency.most_common(15):
    print(f"  {count:>6}  {tool}")
print()

for n in range(2, 6):
    ngram_counts = Counter()
    for seq in sequences:
        for i in range(len(seq) - n + 1):
            ngram_counts[tuple(seq[i : i + n])] += 1
    ranked = sorted(ngram_counts.items(), key=lambda kv: (n - 1) * kv[1], reverse=True)
    print(f"=== Top {top_n} {n}-grams, ranked by (n-1)*count = turns that would be saved ===")
    print(f"{'turns_saved':>12}  {'count':>7}  sequence")
    for ngram, count in ranked[:top_n]:
        print(f"{(n - 1) * count:>12}  {count:>7}  {' -> '.join(ngram)}")
    print()
PYEOF
