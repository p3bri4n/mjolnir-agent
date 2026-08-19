"""
Audit log (Phase 2, blind spot fixed — see docs/history.md, T9
investigation): machine-readable trace of every actually executed
tool_call, any tier, whether it comes from auto_call_tools
(auto-approved) or call_tools (after approval, human or via the campaign
harness — see _execute_tool_calls, app/graph.py). Previously, only
auto_call_tools logged: a turn that went through require_approval was
assumed to be "already traced in the conversation history", a false
assumption in automated campaigns (no human ever looks) and moot anyway
after a service restart (MemorySaver checkpointer, in-memory only) — the
very first call of each tool per thread, the most useful for
investigation, stayed invisible.

TIER_READ calls were excluded until docs/resolved-bugs.md #52 ("silent by
design, nothing new to audit" — true for approval/security purposes, but
it left every wrapper-dispatched read tool, `browser_extract` first among
them, at ZERO occurrences across the entire archive: invisible to any
analysis keyed on the `"tool"` field, including
scripts/analyze-tool-call-ngrams.sh). Now logged like every other tier;
`"tier": "read"` on the entry still lets a consumer filter them back out
if approval/security is specifically what it wants.

Tool result (revised Phase 1d, see docs/history.md "observability
first"): every entry now also carries the result AS SEEN BY THE MODEL
(already truncated/prioritized by _truncate_browser_result on the caller
side — never the raw version, that would duplicate data the model never
received). Without this, the archive could only reconstruct the
SEQUENCE of calls (tool + arguments), never what the agent actually
perceived at each step — which blocked strict verification of
hypotheses 0a/0b during the T5/T8 diagnosis (see docs/history.md). This
is also the foundation for the dashboard's future "agent context"
endpoint.

One JSONL file per day, under AUDIT_LOG_DIR (default /workspace/.audit,
shared with the filesystem/git/terminal MCP servers via the same bind
mount, see docker-compose.yml). Rotation/compression (see
AUDIT_LOG_MAX_BYTES/_rotate_if_needed): persisting results significantly
inflates the volume compared to tool+arguments alone, hence the need to
bound a daily file's size rather than let it grow unbounded.
"""

import gzip
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

AUDIT_LOG_DIR = os.environ.get("AUDIT_LOG_DIR", "/workspace/.audit")

# Beyond this size, the day's log file is compressed and archived
# (suffix ".N.jsonl.gz", increasing N) before a new write starts a fresh
# ".jsonl" file for the same day — the day is therefore no longer
# guaranteed to fit in a single file once this threshold is crossed,
# unlike before tool results were added.
AUDIT_LOG_MAX_BYTES = int(os.environ.get("AUDIT_LOG_MAX_BYTES", str(20 * 1024 * 1024)))


def _log_path_for(when: datetime) -> Path:
    return Path(AUDIT_LOG_DIR) / f"{when.strftime('%Y-%m-%d')}.jsonl"


def _rotate_if_needed(path: Path) -> None:
    if not path.exists() or path.stat().st_size < AUDIT_LOG_MAX_BYTES:
        return
    n = 1
    while (path.parent / f"{path.stem}.{n}.jsonl.gz").exists():
        n += 1
    archive = path.parent / f"{path.stem}.{n}.jsonl.gz"
    with path.open("rb") as src, gzip.open(archive, "wb") as dst:
        shutil.copyfileobj(src, dst)
    path.unlink()


def _append_entry(entry: dict) -> None:
    path = _log_path_for(datetime.now(timezone.utc))
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_if_needed(path)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def log_tool_call(
    thread_id: str, tool_name: str, arguments: dict, tier: str, result: Optional[dict] = None
) -> None:
    _append_entry(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "thread_id": thread_id,
            "tool": tool_name,
            "arguments": arguments,
            "tier": tier,
            "result": result,
        }
    )


def log_message(thread_id: str, role: str, content) -> None:
    """
    Observability (revised Phase 1d, see docs/history.md "extraction fix"
    -> "OBSERVABILITY"): persists the ASSISTANT message (<think> reasoning
    included + final answer, see call_llm/app/graph.py) produced each
    turn — the archive's last missing piece. Without it, an archive
    investigation could only reconstruct what the agent perceived (tool
    results, see log_tool_call) and its action sequence, never its own
    reasoning/text — a limitation honestly flagged several times during
    the T1/T7/T10 diagnosis (see docs/history.md). `kind: "message"`
    distinguishes these entries from tool_calls (`kind` absent for those,
    backward-compatible) on read — see GET /audit, app/main.py, which
    stays deliberately generic (returns everything, filtering is up to
    the consumer).
    """
    _append_entry(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "thread_id": thread_id,
            "kind": "message",
            "role": role,
            "content": content,
        }
    )


def _iter_log_files(root: Path):
    """Daily files, full (.jsonl) then compressed archives (.N.jsonl.gz)
    of the same day, sorted by name (hence in chronological rotation
    order) — see _rotate_if_needed."""
    yield from sorted(root.glob("*.jsonl"))
    yield from sorted(root.glob("*.jsonl.gz"))


def read_entries(thread_id: Optional[str] = None) -> list:
    """
    Reads back all daily files (potentially several if the conversation
    spanned a day change or a volume-based rotation), sorted by
    timestamp, optionally filtered by thread_id. Used by: GET /audit
    (app/main.py). An individual corrupted line is skipped rather than
    failing the whole read — the log stays browsable even if a writer was
    interrupted mid-line.
    """
    root = Path(AUDIT_LOG_DIR)
    if not root.exists():
        return []

    entries = []
    for path in _iter_log_files(root):
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if thread_id is None or entry.get("thread_id") == thread_id:
                    entries.append(entry)

    entries.sort(key=lambda e: e.get("timestamp", ""))
    return entries
