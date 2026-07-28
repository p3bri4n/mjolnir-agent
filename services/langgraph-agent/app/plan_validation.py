"""
Programmatic plan-validation heuristics (Iteration 3, Phase 1 "cognitive
core" — see docs/briefs/phase-1-coeur-cognitif.md). Standalone module,
testable with no docker/LLM/graph state: the sole entry point,
`validate_plan_heuristics`, takes the plan and its context as arguments
rather than fetching them itself.

`_URL_RE` is deliberately DUPLICATED from app/graph.py (not imported):
app/graph.py imports this module to call it from validate_plan — a
reciprocal import would create an import cycle. The duplicate is a
single few-character regex; documented duplication is preferable to
introducing a third module just to host it.
"""

import re
from urllib.parse import urlparse

_URL_RE = re.compile(r"https?://[^\s'\")\]]+")

SUBTASKS_MIN = 2
SUBTASKS_MAX = 12


def _domain(url: str) -> str:
    return urlparse(url).netloc


def validate_plan_heuristics(plan: list, *, known_tools: set, task_scope_urls: set) -> list:
    """
    Returns the rejection reasons (empty list = valid plan):
      - size bounds (SUBTASKS_MIN..SUBTASKS_MAX);
      - no duplicates (description+criterion pair identical to another);
      - referenced tools exist (`known_tools`, langgraph-agent's
        effective schema — see _get_tools_schema, app/graph.py);
      - domains mentioned are within the declared scope (URLs found in
        the subtasks' text, compared by DOMAIN against
        `task_scope_urls` — same page or another path on the same
        allowed site, different domain rejected).

    "No cycles": N/A, not checked — the plan is a sequential list, no
    dependency structure exists for a cycle to even be definable.
    "Tier consistency": verified by construction elsewhere (the plan's
    tier, computed in app/graph.py, derives ONLY from the tools declared
    here — no separate "task" tier to compare against, that concept
    belongs to PLAN.md's Phase 3, not built yet).
    """
    reasons = []
    if not (SUBTASKS_MIN <= len(plan) <= SUBTASKS_MAX):
        reasons.append(
            f"nombre de sous-tâches hors bornes ({len(plan)}, attendu {SUBTASKS_MIN}-{SUBTASKS_MAX})"
        )

    scope_domains = {_domain(u) for u in task_scope_urls if _domain(u)}
    seen = set()
    for i, subtask in enumerate(plan):
        key = (subtask.get("description"), subtask.get("success_criterion"))
        if key in seen:
            reasons.append(f"sous-tâche {i} dupliquée (description+critère identiques à une autre)")
        seen.add(key)

        for tool in subtask.get("tools", []):
            if tool not in known_tools:
                reasons.append(f"sous-tâche {i} référence un outil inconnu : {tool}")

        text = f"{subtask.get('description', '')} {subtask.get('success_criterion', '')}"
        for url in _URL_RE.findall(text):
            domain = _domain(url)
            if scope_domains and domain and domain not in scope_domains:
                reasons.append(f"sous-tâche {i} référence un domaine hors périmètre : {domain}")

    return reasons
