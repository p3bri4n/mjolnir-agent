"""
Approval policy by reversibility tier.

Replaces the historical binary whitelist (AUTO_APPROVED_TOOLS: auto or
not) with three tiers, from least to most risky:

  TIER_READ       : auto, silent. Pure introspection or read-only —
                    nothing to exfiltrate, nothing to undo.
  TIER_REVERSIBLE : auto + logging (see Phase 2, audit log). Side
                    effect, but reversible and confined (filesystem
                    writes under /workspace).
  TIER_SENSITIVE  : human approval required. Free-text input, everything
                    else, AND any unknown tool — the default is ALWAYS
                    the most restrictive tier, never the reverse: a tool
                    that appears in none of the lists below is NOT
                    auto-approved.

Routing (see has_tool_calls, app/graph.py): a turn is auto-approved only
if ALL its tool_calls are tier 1 or 2 — a single sensitive-tier tool
(even mixed with auto-approved tools) submits the whole turn for
approval, no per-tool partial approval.

Backward compatibility: AUTO_APPROVED_TOOLS (the old env var) still
works as an override — any tool listed there is treated as tier 2 (auto
+ audit) even if it's in none of the default lists below.
"""

import os
from typing import Optional

TIER_READ = "read"
TIER_REVERSIBLE = "reversible"
TIER_SENSITIVE = "sensitive"

# Increasing restriction order, used to arbitrate ambiguities (Phase 4:
# several rules matching the same tool) — the most restrictive tier
# always wins.
_TIER_RANK = {TIER_READ: 0, TIER_REVERSIBLE: 1, TIER_SENSITIVE: 2}

# Pure introspection (no side effect) and read-only: nothing to
# exfiltrate, nothing to undo. Reuses the read tools of the official
# filesystem MCP server plus the browser's own read-only tools.
_DEFAULT_TIER_READ = {
    # Targeted location/extraction in the page (revised Phase 1d, see
    # docs/history.md "extraction fix"): pure read despite its internal
    # implementation via browser_evaluate (mcp-client) — the model only
    # supplies a text to search for, never code (see
    # services/mcp-client/app/main.py, _build_extract_function: FIXED JS
    # template, query interpolated via json.dumps).
    "browser_extract",
    # DOM introspection when a ref/selector doesn't resolve (B-β hard
    # finding, docs/resolved-bugs.md "défaut ref= browser_fill_form"):
    # same movement as browser_extract above — a FIXED JS template
    # (services/mcp-client/app/main.py, _build_inspect_call), never
    # model-supplied code, so the legitimate fallback for "what are this
    # form's real attributes" no longer has to go through browser_evaluate
    # (TIER_SENSITIVE/NEVER_GRANTABLE).
    "browser_inspect",
    # Pure observation, no side effect (2026-07-31, found while probing
    # visual-channel feasibility, docs/architecture/visual-channel-
    # feasibility.md): both declared `type: "readOnly"` by the official
    # Playwright MCP server itself (verified against the installed
    # mcp/playwright:latest schema, CLAUDE.md #8), yet defaulted to
    # TIER_SENSITIVE — an approval pause for looking at a page, not
    # acting on it. Same reasoning as browser_extract/browser_inspect
    # above. Approval tiers are measured behavior (CLAUDE.md): this
    # change needs its own restricted smoke before any campaign compares
    # against it — see docs/resolved-bugs.md for that smoke's result.
    "browser_snapshot",
    "browser_take_screenshot",
    "read_file",
    "read_multiple_files",
    "list_directory",
    "directory_tree",
    "search_files",
    "get_file_info",
    "list_allowed_directories",
}

# Reversible and confined side effect: filesystem writes under
# /workspace.
_DEFAULT_TIER_REVERSIBLE = {
    "write_file",
    "edit_file",
    "create_directory",
    "move_file",
}

# Never session-grantable (revised Phase 1d, see docs/history.md, T5):
# arbitrary code execution in the page (unconstrained JS) — an
# escalation, not a read primitive, no matter how many times a human has
# already approved it in this thread. These two tools stay
# TIER_SENSITIVE by default (absent from every list above); what
# NEVER_GRANTABLE_TOOLS adds is forbidding the relaxation normally
# allowed by a session grant (see effective_tier) — "approve for the
# session" has no effect on these two: every call requires an explicit,
# individual approval.
#
# NEVER_GRANTABLE_TOOLS_EXTRA (docs/briefs/B3-benchmark-v2.md, family B —
# the "medium"/"hard" policy loads' "no ENGAGEMENT action without
# individual approval, session grant does not cover it"): additive,
# comma-separated, empty by default — zero behavior change for any
# deployment that doesn't set it. Exists because RULES/APPROVAL_RULES_PATH
# (see Phase 4 below) only overrides a call's TIER, it does NOT exempt a
# tool from grant-relaxation — the two mechanisms are deliberately
# separate (a rule can downgrade a tool to reversible; that must still
# be relaxable by a grant like everything else at that tier). Per-campaign
# knob, not a permanent default: a benchmark task that must force
# individual approval on a specific tool (e.g. browser_click, if that
# tool is what performs the task's one engagement action) sets this via
# docker-compose/.env for that campaign only.
NEVER_GRANTABLE_TOOLS = {"browser_run_code_unsafe", "browser_evaluate"} | set(
    filter(None, os.environ.get("NEVER_GRANTABLE_TOOLS_EXTRA", "").split(","))
)


def _load_tier_override(env_var: str, default: set) -> set:
    raw = os.environ.get(env_var)
    if raw is None:
        return set(default)
    return set(filter(None, raw.split(",")))


TIER_READ_TOOLS = _load_tier_override("TIER_READ_TOOLS", _DEFAULT_TIER_READ)
TIER_REVERSIBLE_TOOLS = _load_tier_override("TIER_REVERSIBLE_TOOLS", _DEFAULT_TIER_REVERSIBLE)

# Backward-compatible override: a tool listed here is treated as tier 2
# (auto + audit) even if it appears in neither list above. Empty by
# default — AUTO_APPROVED_TOOLS's old defaults are now already covered by
# _DEFAULT_TIER_READ/_DEFAULT_TIER_REVERSIBLE, so this new empty default
# reproduces the same behavior for a deployment that doesn't set this
# variable.
AUTO_APPROVED_TOOLS = set(filter(None, os.environ.get("AUTO_APPROVED_TOOLS", "").split(",")))



# Local meta-tool (latency fix 1/2-bis, app/graph.py:
# _REPORT_AND_ACT_TOOL): never served by mcp-client, hence deliberately
# NOT added to _DEFAULT_TIER_READ/TIER_READ_TOOLS — these sets also feed
# EXPECTED_TOOLS (tests_integration/campaign_preflight.py), compared
# term-by-term against mcp-client's REAL schema; adding it there would
# fail the campaign preamble ("expected" tool that will never appear in
# that schema). Classified directly here instead, as pure read (no side
# effect, never TIER_SENSITIVE) — without which has_tool_calls()
# (app/graph.py) would wrongly submit EVERY verified turn to human
# approval (tool_tier()'s TIER_SENSITIVE default for any unknown name).
REPORT_AND_ACT_TOOL_NAME = "report_and_act"


def tool_tier(tool_name: str) -> str:
    """Static tier of a tool, ignoring session grants (Phase 3) or
    argument rules (Phase 4) — see effective_tier() for the full
    resolution. Default = TIER_SENSITIVE."""
    if tool_name == REPORT_AND_ACT_TOOL_NAME:
        return TIER_READ
    if tool_name in TIER_READ_TOOLS:
        return TIER_READ
    if tool_name in TIER_REVERSIBLE_TOOLS or tool_name in AUTO_APPROVED_TOOLS:
        return TIER_REVERSIBLE
    return TIER_SENSITIVE


def effective_tier(tool_name: str, args=None, session_grants=None) -> str:
    """
    Real tier of a specific tool_call (name + arguments) for THIS thread:

      1. Argument rules (Phase 4, see below): if at least one rule named
         for this tool matches these arguments, its tier overrides the
         tool's static tier — NOT a logical AND with it. If several rules
         match with different tiers, the most restrictive wins
         (ambiguity).
      2. Otherwise, the tool's static tier (tool_tier(), ignoring
         arguments and grants).
      3. Session grants (Phase 3, see AgentState.session_grants in
         app/graph.py): if the result of the two previous steps is
         TIER_SENSITIVE and the tool is in session_grants, capped at
         TIER_REVERSIBLE. A grant can only relax, never tighten — a tool
         already TIER_READ/TIER_REVERSIBLE is unaffected by this step.
    """
    rule_tier = _match_rules(tool_name, args or {})
    resolved = rule_tier if rule_tier is not None else tool_tier(tool_name)
    if (
        resolved == TIER_SENSITIVE
        and session_grants
        and tool_name in session_grants
        and tool_name not in NEVER_GRANTABLE_TOOLS
    ):
        return TIER_REVERSIBLE
    return resolved


def is_auto_approved(tool_name: str, args=None, session_grants=None) -> bool:
    return effective_tier(tool_name, args, session_grants) in (TIER_READ, TIER_REVERSIBLE)


# ─────────────────────────────────────────────────────────────────────────
# Phase 4: argument rules ("tool(pattern)", Claude-Code style) — let a
# tool's tier be refined based on ITS ARGUMENTS rather than just its name.
# Deliberately minimal implementation: matchers named in Python (no
# generic pattern DSL to parse/validate), a rule table below,
# overridable/extendable via an optional YAML file (APPROVAL_RULES_PATH).
# ─────────────────────────────────────────────────────────────────────────


class Rule:
    __slots__ = ("tool", "matcher", "tier")

    def __init__(self, tool: str, matcher, tier: str):
        self.tool = tool
        self.matcher = matcher
        self.tier = tier


def _matcher_any(args: dict) -> bool:
    return True


def _matcher_command_prefix(prefixes):
    """A tool's free-text `command` argument matched by prefix, e.g.
    tool_name(prefix:some_command). No default rule uses this matcher —
    kept for APPROVAL_RULES_PATH overrides (a deployment-specific tool
    with a command-like argument)."""

    def _match(args: dict) -> bool:
        command = args.get("command", "")
        return any(command == p or command.startswith(p + " ") for p in prefixes)

    return _match


# Registry of matchers overridable by name from a YAML file (see
# _load_rules_from_yaml) — "command_prefix" expects a parameter
# ("prefixes"), the others are used as-is.
_MATCHER_REGISTRY = {
    "any": _matcher_any,
    "command_prefix": _matcher_command_prefix,
}

# No default argument rule needed today — kept as an empty list, refined
# via APPROVAL_RULES_PATH for deployment-specific needs (see Phase 4
# above).
DEFAULT_RULES = []


def _load_rules_from_yaml(path: str) -> list:
    import yaml  # lazy import: only deployments setting APPROVAL_RULES_PATH need it

    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    rules = []
    for item in data.get("rules", []):
        matcher_name = item["matcher"]
        factory = _MATCHER_REGISTRY[matcher_name]
        matcher = factory(item["prefixes"]) if matcher_name == "command_prefix" else factory
        rules.append(Rule(item["tool"], matcher, item["tier"]))
    return rules


def _load_rules() -> list:
    rules = list(DEFAULT_RULES)
    path = os.environ.get("APPROVAL_RULES_PATH")
    if path:
        rules += _load_rules_from_yaml(path)
    return rules


RULES = _load_rules()


def _match_rules(tool_name: str, args: dict) -> Optional[str]:
    matched = [r.tier for r in RULES if r.tool == tool_name and r.matcher(args)]
    if not matched:
        return None
    return max(matched, key=lambda t: _TIER_RANK[t])
