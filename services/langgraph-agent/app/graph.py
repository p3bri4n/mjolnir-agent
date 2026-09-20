"""
LangGraph orchestration graph.

Flow:
  1. retrieve_context   -> queries the Context Manager (RAG / memory)
  2. select_skill        -> queries the Skill Manager to inject a relevant skill prompt
  3. validate_plan        -> validates `state["plan"]` if PLAN_VALIDATION_ENABLED
     and one exists (kept as a safety-value pipeline — see
     revise_plan/require_plan_approval/reject_plan below — even though
     the only current writer, PLANNING_MODE="merged"'s manage_plan tool,
     does its own inline check and never reaches it in practice; no-op
     otherwise, same flow as before this pipeline existed)
  4. call_llm             -> calls the inference backend (TabbyAPI by
     default, OpenAI-compatible API) with function calling
  6. has_tool_calls       -> routes to require_approval, or directly to
     auto_call_tools if ALL the turn's tool_calls are auto-approved per
     the per-tier policy (app/approval_policy.py, see below)
  7. require_approval (optional) -> if the LLM requests a non-auto-approved
     tool, pauses the graph (NodeInterrupt) until a human has
     approved/refused via the "approved" state
  8. call_tools | auto_call_tools | reject_tools -> runs the tool via the
     MCP Client (same shared logic, see _execute_tool_calls), or
     synthesizes a refusal if the human refused. Both log to the audit
     log (Phase 2, see app/audit_log.py) any tier other than TIER_READ,
     then loop back to call_llm.
  9. END                  -> final answer

Human supervision: by default, every tool call is subject to approval
(see require_approval/reject_tools below), except for tools classified as
"read" or "reversible" tier by app/approval_policy.py (browser/filesystem
reads and writes, by default — see that module for the tier detail). The
graph is therefore compiled with a checkpointer
(MemorySaver, in-memory) so it can suspend then resume execution — at the
cost of losing pending approvals if the service restarts (acceptable for
local use, see README).
"""

import base64
import contextvars
import difflib
import io
import logging
import math
import os
import json
import re
import shlex
import uuid
import zoneinfo
from datetime import datetime
from typing import Annotated, Optional, TypedDict
from urllib.parse import urljoin

import httpx
import langchain_openai.chat_models.base as _openai_base
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from PIL import Image
from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import NodeInterrupt
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages

from app import approval_policy, audit_log, plan_validation

logger = logging.getLogger(__name__)

# A "thinking" model's reasoning arrives in a dedicated field of the SSE
# deltas, alongside "content" — outside the standard OpenAI format, which
# langchain-openai silently ignores (_convert_delta_to_message_chunk only
# reads "content"/"tool_calls"/"function_call"). The NAME of this field
# differs by backend: "reasoning" with Ollama (Qwen3+ models),
# "reasoning_content" with llama-server (confirmed under real conditions
# with the turboquant-webp fork serving Qwen3.6 — llama-server follows the
# DeepSeek-R1/OpenAI o1 convention here, not Ollama's). Without handling
# both names, reasoning streamed by llama-server would silently disappear
# (no error, just absent from the stream) — verified via a real streamed
# call before this fix: the deltas only ever contained "reasoning_content",
# never "reasoning". We reinject whatever content is found (regardless of
# the field name) by folding it into "content", wrapped in
# <think>...</think> (a convention recognized by Open WebUI to display a
# collapsible thinking bubble), which makes it show up in the existing
# streaming flow without touching app/main.py.
#
# TabbyAPI (the default backend since the ExLlamaV3 migration, see README
# section Inference backend) has its own `reasoning: true` toggle on the
# config.yml side, but the NAME of the SSE field it emits on the wire
# hasn't been empirically verified yet (see
# tests_integration/CUDA-DIAGNOSTIC.md / tabbyapi implementation plan, open
# risk #3) — if neither "reasoning" nor "reasoning_content" match under
# real conditions, add a third `or _dict.get(...)` below once the real
# name is confirmed by a real streamed call, not guessed.
_think_state: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "_think_state", default=None
)
_original_convert_delta = _openai_base._convert_delta_to_message_chunk


def _convert_delta_with_reasoning(_dict, default_class):
    chunk = _original_convert_delta(_dict, default_class)
    state = _think_state.get()
    if state is None:
        return chunk
    reasoning = _dict.get("reasoning") or _dict.get("reasoning_content")
    real_content = chunk.content
    if reasoning:
        prefix = "<think>" if not state["opened"] else ""
        state["opened"] = True
        pieces = [prefix, reasoning]
        if real_content:
            # This delta contains both the end of the reasoning AND the
            # start of the final answer in the SAME chunk (observed with
            # TabbyAPI/ExLlamaV3 — llama-server/Ollama always kept the two
            # in separate chunks, hence this bug being invisible before
            # this migration). Without this case, chunk.content being
            # overwritten by the reasoning alone right below would
            # silently drop the real answer — the turn would then end
            # with no visible content, wrongly triggering the
            # empty-answer safety net.
            state["closed"] = True
            pieces.append("</think>\n\n")
            pieces.append(real_content)
        chunk.content = "".join(pieces)
    elif chunk.content and state["opened"] and not state["closed"]:
        state["closed"] = True
        chunk.content = "</think>\n\n" + chunk.content
    return chunk


_openai_base._convert_delta_to_message_chunk = _convert_delta_with_reasoning

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://tabbyapi:5000/v1")
CONTEXT_MANAGER_URL = os.environ.get("CONTEXT_MANAGER_URL", "http://context-manager:8002")
SKILL_MANAGER_URL = os.environ.get("SKILL_MANAGER_URL", "http://skill-manager:8001")
MCP_CLIENT_URL = os.environ.get("MCP_CLIENT_URL", "http://mcp-client:8003")
OCR_SERVICE_URL = os.environ.get("OCR_SERVICE_URL", "http://ocr-service:8004")

# docs/briefs/visual-navigation-only.md (effort 8): capture + OCR only, no
# DOM/accessibility tree, coordinate-based action space. Default false:
# unchanged behavior, existing DOM mode. See _VISUAL_ONLY_BLOCKED_TOOLS/
# _VISION_ONLY_TOOLS (_get_bound_llm) and _ocr_replace_image_blocks
# (_call_mcp_tool) for the two places this actually changes behavior.
VISUAL_NAVIGATION_ONLY = os.environ.get("VISUAL_NAVIGATION_ONLY", "false").lower() == "true"

# Hard-gated when VISUAL_NAVIGATION_ONLY is active: every tool that reads
# the DOM/accessibility tree or executes/inspects page internals — a
# sighted human looking only at the rendered screenshot has none of this.
# Verified against the real @playwright/mcp catalog (Phase 1 of the brief
# above), not assumed from memory.
_VISUAL_ONLY_BLOCKED_TOOLS = {
    "browser_snapshot",
    "browser_find",
    "browser_click",
    "browser_hover",
    "browser_drag",
    "browser_drop",
    "browser_select_option",
    "browser_type",
    "browser_fill_form",
    "browser_evaluate",
    "browser_run_code_unsafe",
    "browser_extract",
    "browser_inspect",
    "browser_console_messages",
    "browser_network_request",
    "browser_network_requests",
}
# Coordinate-based action space (playwright-mcp's "vision" capability,
# --caps=vision — docker-compose.yml): the counterpart to the tools
# blocked above. Only meaningful under VISUAL_NAVIGATION_ONLY — hidden
# otherwise to keep the default mode's schema weight unchanged (effort
# 1.1/1.2's -44.9% is not something to give back for free).
_VISION_ONLY_TOOLS = {
    "browser_mouse_click_xy",
    "browser_mouse_move_xy",
    "browser_mouse_drag_xy",
    "browser_mouse_down",
    "browser_mouse_up",
    "browser_mouse_wheel",
}

# URL-fabrication guardrail (Phase 1, see PLAN.md/docs/history.md — target
# #1 of the Phase 0 point zero: the agent regularly invents plausible URLs
# it never observed — page-4.html on a 3-page catalog, a nonexistent
# search path... — rather than following a real link from the DOM).
BROWSER_NAVIGATE_GUARDRAIL = os.environ.get("BROWSER_NAVIGATE_GUARDRAIL", "true").lower() == "true"

# Graduated feedback (Phase 1c, see docs/history.md): 1b (the full link
# list on EVERY rejection) made T4/T7/T8 regress compared to 1a — the
# full list was redundant (already in the structured snapshot) and
# weighed down every rejection. Three tiers by NUMBER OF FABRICATED
# ATTEMPTS for this task (not per subtask — the full Phase 1, not yet
# done, will introduce this finer breakdown):
#   1-2: minimal message, no list (the snapshot already has it).
#   3..LIMIT-1: + a few links closest to the fabricated URL
#                (targeted help, not a directory).
#   >=LIMIT: the feedback changes nature — pushes toward an honest
#             conclusion of absence rather than yet another guess
#             (bridge to T7: persistence becomes a legitimate admission
#             of failure).
FABRICATION_LIMIT = int(os.environ.get("FABRICATION_LIMIT", "5"))

def _fabrication_feedback(fabricated_url: str, attempt_number: int, page_links: list) -> str:
    if attempt_number >= FABRICATION_LIMIT:
        # Cap (Phase 1c): conditional redirection to "strong candidates"
        # tried in Phase 1d then SUSPENDED (see docs/history.md) — the
        # hypothesis behind this branch (0a, T5/T8 archive check) wasn't
        # confirmed by the sequences actually observed. Reverts to 1c's
        # unconditional message: at the cap, concluding absence is a valid
        # answer, full stop. The real T5 fix now lives on the infra side
        # (dedicated download volume, see docs/history.md
        # "Phase 1d-revised") rather than in a similarity heuristic on
        # this feedback.
        return (
            f"URL non observée (tentative n°{attempt_number}). Plusieurs tentatives vers des URL "
            "inexistantes. Si la cible ne figure dans aucune page observée, conclure qu'elle "
            "est introuvable est une réponse valide — ne continue pas à deviner des chemins."
        )
    if attempt_number >= 3:
        closest = difflib.get_close_matches(fabricated_url, page_links, n=8, cutoff=0.0)[:8]
        liens_txt = "\n".join(f"- {u}" for u in closest) or "(aucun lien connu pour l'instant)"
        return (
            f"URL non observée dans la page (tentative n°{attempt_number}) — utilise un lien "
            f"réellement présent dans le snapshot. Liens les plus proches de ce que tu cherchais :\n{liens_txt}"
        )
    return (
        "URL non observée sur cette page. Utilise un lien réellement présent dans le snapshot "
        "(l'inventaire complet des liens y figure déjà) — ne devine pas un chemin."
    )


_URL_RE = re.compile(r"https?://[^\s'\")\]]+")
_SNAPSHOT_URL_LINE_RE = re.compile(r"/url:\s*(\S+)")
_PAGE_URL_LINE_RE = re.compile(r"Page URL:\s*(\S+)")

# Tool-output bound (Phase 1): an oversized browser_* tool result (a dense
# real page, see T8/T11 — LLM context overflow discovered under real
# conditions, see docs/history.md) is truncated at the SOURCE, before
# entering the conversation history. Distinct from image retention (Phase
# 2, MAX_IMAGES_IN_CONTEXT): this bounds the size of a SINGLE tool result,
# not the whole history.
BROWSER_TOOL_OUTPUT_MAX_CHARS = int(os.environ.get("BROWSER_TOOL_OUTPUT_MAX_CHARS", "8000"))


def _clean_url(url: str) -> str:
    """Strips trailing sentence punctuation mistakenly glued to the match
    (e.g. "http://example.com/page.html," in a French sentence) — a real
    URL normally never ends with these characters."""
    return url.rstrip(",.;:")


def _extract_urls(text: str, base_url: Optional[str]) -> set:
    """Absolute and relative URLs (resolved via base_url) found in a
    browser_* tool result's text (Playwright snapshot in YAML format,
    "- /url: ...", or free text containing absolute URLs)."""
    found = {_clean_url(m) for m in _URL_RE.findall(text)}
    for match in _SNAPSHOT_URL_LINE_RE.findall(text):
        match = _clean_url(match)
        found.add(urljoin(base_url, match) if base_url else match)
    return found


def _extract_page_url(text: str) -> Optional[str]:
    match = _PAGE_URL_LINE_RE.search(text)
    return match.group(1) if match else None


def _task_scope_urls(messages: list) -> set:
    """Roots of the task's scope: URLs mentioned in the first human
    message (see tests_integration/test_web_tasks.py, prompt convention —
    a task always mentions the target site's URL)."""
    first_human = next((m for m in messages if getattr(m, "type", None) == "human"), None)
    if first_human is None or not isinstance(first_human.content, str):
        return set()
    return {_clean_url(m) for m in _URL_RE.findall(first_human.content)}


_AFFORDANCE_LINE_RE = re.compile(r'-\s*\'?(link|button|textbox|combobox|checkbox|option)\s+"([^"]*)"')

# Tiered inventory (Phase 1d, point 2): beyond this number of
# affordances, preserving the FULL list becomes counterproductive — on a
# real Wikipedia page (593 affordances, ~47000 characters for the
# inventory alone), the inventory already far exceeded the output cap and
# starved out ALL the descriptive content, including the semantic link
# between "Naissance" and "Muret" (see docs/history.md, T8 archive
# check).
AFFORDANCE_THRESHOLD = int(os.environ.get("AFFORDANCE_THRESHOLD", "60"))
_NAV_KEYWORDS = {
    "suivant", "précédent", "precedent", "next", "previous", "prev", "page",
    "retour", "accueil", "home", "sommaire", "menu", "navigation",
}


def _is_nav_label(label: str) -> bool:
    lowered = label.lower()
    return any(kw in lowered for kw in _NAV_KEYWORDS)


def _extract_affordances_structured(text: str) -> list[dict]:
    """
    Structured inventory of a snapshot's INTERACTIVE elements (links with
    href, buttons, form fields). A "link/button/..." line is followed (in
    the next 2 lines, Playwright format) by a "- /url: ..." line if the
    element has a target; otherwise (button, field) it's listed without a
    URL.
    """
    lines = text.splitlines()
    items = []
    for i, line in enumerate(lines):
        match = _AFFORDANCE_LINE_RE.search(line)
        if not match:
            continue
        kind, label = match.groups()
        url = None
        for lookahead in lines[i + 1 : i + 3]:
            url_match = _SNAPSHOT_URL_LINE_RE.search(lookahead)
            if url_match:
                url = _clean_url(url_match.group(1))
                break
        items.append({"kind": kind, "label": label, "url": url})
    return items


def _format_affordance(item: dict) -> str:
    # Keeps the literal "/url: <target>" pattern (not "-> url"): this
    # block later goes back through _extract_urls (see
    # _execute_tool_calls), which specifically recognizes this pattern
    # for relative links — any other format would be invisible to it and
    # would break observed_urls tracking on any truncated result.
    return f'- {item["kind"]} "{item["label"]}"' + (f' /url: {item["url"]}' if item["url"] else "")


def _extract_affordances(text: str) -> list[str]:
    """See _truncate_browser_result: this inventory is ALWAYS kept in full
    below AFFORDANCE_THRESHOLD elements — beyond that, _prioritize_affordances
    tiers it instead of keeping everything (see this module, docs/history.md,
    "truncation starves navigation")."""
    return [_format_affordance(i) for i in _extract_affordances_structured(text)]


def _prioritize_affordances(items: list[dict], objective: str) -> tuple[list[str], int]:
    """
    Beyond AFFORDANCE_THRESHOLD: pagination/navigation ALWAYS stays in
    full (never the bottleneck), content-area links are sorted by
    proximity to the current task's objective (the initial prompt, for
    lack of explicit subtasks — full Phase 1 not done yet) and capped;
    the rest is counted, not listed.
    """
    nav = [i for i in items if _is_nav_label(i["label"])]
    content = [i for i in items if not _is_nav_label(i["label"])]
    if objective:
        content.sort(
            key=lambda i: difflib.SequenceMatcher(None, i["label"].lower(), objective.lower()).ratio(),
            reverse=True,
        )
    kept_content = content[:AFFORDANCE_THRESHOLD]
    elided = len(content) - len(kept_content)
    lines = [_format_affordance(i) for i in nav] + [_format_affordance(i) for i in kept_content]
    return lines, elided


def _truncate_browser_result(result: dict, max_chars: int, objective: str = "") -> dict:
    """
    Truncates an oversized browser_* tool result WITHOUT ever losing the
    RELEVANT affordances inventory (see _extract_affordances /
    _prioritize_affordances): it's placed at the top, before the content
    (potentially truncated). The max_chars budget applies to the
    CONTENT, not the inventory — if the inventory (already tiered if
    needed) still exceeds max_chars, it stays whole: preserving
    navigation takes priority over strictly respecting the cap in this
    rare case.
    """
    content = result.get("content")
    if not isinstance(content, list):
        return result
    new_content = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and len(block.get("text", "")) > max_chars:
            structured = _extract_affordances_structured(block["text"])
            if len(structured) > AFFORDANCE_THRESHOLD:
                formatted, elided = _prioritize_affordances(structured, objective)
                elided_note = (
                    f"\n(+ {elided} liens de contenu supplémentaires non affichés, triés par pertinence)"
                    if elided
                    else ""
                )
            else:
                formatted, elided_note = [_format_affordance(i) for i in structured], ""
            page_url = _extract_page_url(block["text"])
            # La ligne "Page URL: ..." elle-même est préservée en tête,
            # jamais tronquée : nécessaire pour résoudre les liens relatifs
            # de l'inventaire ci-dessous (voir _extract_urls, base_url).
            page_url_line = f"Page URL: {page_url}\n" if page_url else ""
            affordances_block = (
                (
                    page_url_line
                    + "### Éléments interactifs (liens/boutons/champs)\n"
                    + "\n".join(formatted)
                    + elided_note
                    + "\n\n"
                )
                if formatted
                else page_url_line
            )
            remaining = max(max_chars - len(affordances_block), 0)
            block = {
                **block,
                "text": (
                    affordances_block
                    + block["text"][:remaining]
                    + f"\n[...contenu tronqué à {remaining} caractères (éléments interactifs ci-dessus préservés)...]"
                ),
            }
        new_content.append(block)
    return {**result, "content": new_content}

# Format sent to the LLM for tool image results (browser_take_screenshot,
# native WebP format): empty (the default) always re-encodes to PNG — the
# default backend (TabbyAPI/ExLlamaV3, see README section Inference
# backend) is not known to decode WebP natively (to be verified
# empirically, see the tabbyapi implementation plan, open risk #2;
# necessary anyway with Ollama, whose mtmd decoder explicitly fails on
# WebP). "webp" only activates with the alternative llama-server backend,
# whose llama.cpp fork decodes WebP natively (see _to_png_data_uri below
# and the README's bug table).
IMAGE_FORMAT_PASSTHROUGH = os.environ.get("IMAGE_FORMAT_PASSTHROUGH", "").lower() == "webp"

# Cumulative tool-call budget for a single task: shared across a
# thread's whole approval chain, NOT reset to zero between two
# "approve" turns (tool_iterations only starts back at 0 on a brand-new
# user message, see _resolve_run in app/main.py) — an old default of 5
# used to run out after barely 2-3 approval round-trips, before even
# reaching a long auto-approved read/reversible tool loop. Overflow
# reported explicitly to the user rather than silently (see
# _current_answer, app/main.py).
MAX_TOOL_ITERATIONS = int(os.environ.get("MAX_TOOL_ITERATIONS", "20"))

# Approval policy by reversibility tier (see app/approval_policy.py): a
# turn is auto-approved if ALL its tool_calls are "read" or "reversible"
# tier; a mixed turn (even a single "sensitive"-tier tool) stays fully
# subject to approval, for safety — no partial per-tool approval.
# AUTO_APPROVED_TOOLS (old env var) keeps working as a backward-compatible
# override, handled in approval_policy.tool_tier().

# Number of consecutive auto-approved turns tolerated before forcing a
# pass through require_approval anyway, even if all the turn's tool_calls
# remain auto-approved ("read"/"reversible" tier) — defense in depth
# against a long unsupervised streak composing an unintended outcome
# through many individually-harmless auto-approved steps, never fully
# reviewed by a human. Reset to 0 on every
# real pass through require_approval (see this function below), not just
# at the start of a new task — unlike tool_iterations, which measures a
# total budget rather than a number of consecutive turns WITHOUT human
# supervision.
AUTO_APPROVAL_STREAK_LIMIT = int(os.environ.get("AUTO_APPROVAL_STREAK_LIMIT", "6"))

# Image retention in the history submitted to the LLM: every
# browser_take_screenshot capture adds a multimodal message costly in
# visual tokens (see _split_image_blocks); on a repeated capture loop,
# keeping ALL of them ends up saturating the context for near-zero value
# (only the most recent capture reflects the current visual state).
# Keeps only the last MAX_IMAGES_IN_CONTEXT images in what's sent to the
# LLM; earlier ones are replaced by a placeholder text — only for THIS
# call (see _apply_image_retention), never persisted in the graph's
# state/checkpointer: the full history (with all original images) stays
# unchanged and replayable/inspectable.
MAX_IMAGES_IN_CONTEXT = int(os.environ.get("MAX_IMAGES_IN_CONTEXT", "1"))
IMAGE_RETENTION_PLACEHOLDER = "[screenshot antérieure supprimée]"

# Episode compaction (Phase 2, PLAN.md): same transient-filter principle as
# image retention above (only what's sent to the LLM, never the
# checkpointer/audit log) — beyond EPISODE_COMPACTION_TURN_THRESHOLD
# messages, a completed subtask's raw turns are replaced by one structured
# summary (see _apply_episode_compaction). Ships OFF by default, like
# PLANNER_ENABLED originally did (docs/briefs/flags-du-coeur-cognitif.md):
# flip to "true" only after its own single-variable validation campaign
# (CLAUDE.md, Measured behavior). EPISODE_COMPACTION_TURN_THRESHOLD's
# default (40) is a starting point for that campaign, not a calibrated
# value.
EPISODE_COMPACTION_ENABLED = os.environ.get("EPISODE_COMPACTION_ENABLED", "false").lower() == "true"
EPISODE_COMPACTION_TURN_THRESHOLD = int(os.environ.get("EPISODE_COMPACTION_TURN_THRESHOLD", "40"))

# History diff (Effort 2, docs/briefs/scaffolding-optimisation.md): same
# transient-filter principle as image retention/episode compaction above
# (only what's sent to the LLM, never the checkpointer/audit log) — every
# PAST browser_* tool result (all but the most recent) is replaced by a
# short structural diff against its nearest predecessor, instead of a
# repeated full snapshot. No threshold var: unlike episode compaction the
# boundary here is structural ("not the latest"), not a message count.
# Ships OFF by default; flip only after its own single-variable
# validation campaign (CLAUDE.md, Measured behavior).
HISTORY_DIFF_ENABLED = os.environ.get("HISTORY_DIFF_ENABLED", "false").lower() == "true"

# Planner node, post-action verification, and the LLM plan judge
# (Iteration 1/2/3, Phase 1 "cognitive core") were removed (not just
# disabled) after the decisive cfg1-vs-cfg8 ablation (36 runs,
# discriminating 5-task subset) found cfg1 (all 4 flags off) never
# losing to cfg8 (all on) at 43% less cumulative time for essentially
# identical real work — and the A1 trajectory diagnostic plus
# `docs/resolved-bugs.md` #51 both found the mechanism actively
# discarding genuine progress via attempt/replan-budget churn on
# multi-page tasks, not merely costing more for the same result
# (PLAN.md, Effort 2; docs/resolved-bugs.md #61's removal PR). Their
# code (plan_task, verify_action, replan_task, report_failure, the
# report_and_act/constat_precedent mechanism, the LLM plan judge) is
# gone; see git history for the design (docs/briefs/archives/
# coeur-cognitif.md, docs/briefs/archives/flags-du-coeur-cognitif.md) if
# ever revisited.
#
# Plan validation pipeline (Iteration 3, Phase 1 "cognitive core" — see
# docs/briefs/archives/coeur-cognitif.md and app/plan_validation.py) KEPT
# as a safety-value exception (a programmatic heuristic gate, not a
# score-driven mechanism — untouched by the CuP reading above): validates
# `state["plan"]` whenever one exists (currently only ever populated by
# PLANNING_MODE="merged"'s own manage_plan tool, which does its own
# inline heuristic check and never reaches this pipeline in practice —
# see revise_plan/require_plan_approval/reject_plan below, kept as the
# same safety net for a plan set any other way).
PLAN_VALIDATION_ENABLED = os.environ.get("PLAN_VALIDATION_ENABLED", "true").lower() == "true"
# "Justified rejection → back to the planner, max 2 cycles then human
# escalation" (brief): number of rejections tolerated before a human
# decides (require_plan_approval, with the rejection reasons displayed)
# rather than looping indefinitely.
PLAN_VALIDATION_CYCLES_MAX = 2

# Effort 2 point 3 (docs/briefs/update-plan.md, "2.1 addendum") — 5th
# cognitive-core condition: planning as an action in the main turn
# (manage_plan tool below) instead of the removed 4-flag dedicated nodes,
# targeting the auxiliary-call latency the 4-flag ablation attributed to
# them. Value-selected mode (like IMAGE_FORMAT_PASSTHROUGH above), not a
# plain on/off gate: this is the first 2-way string mode in this file
# rather than a boolean. Default "nodes" = current behavior, byte-for-byte
# unchanged. In "merged" mode, validate_plan/revise_plan/require_plan_
# approval still run on whatever manage_plan sets, but in practice never
# see anything to reject (manage_plan's own set_plan does its own inline
# heuristic check first, see _execute_tool_calls) — all planning
# responsibility moves into manage_plan alone.
PLANNING_MODE = os.environ.get("PLANNING_MODE", "nodes")

# Qwen models reason by default on every turn (extended thinking) —
# useful for an initial decision, costly in latency/tokens for a fast
# perception-action loop (capture -> click -> capture...) where each turn
# only has to decide "where to click next" without reconsidering the
# whole task. If ADAPTIVE_THINKING is enabled, thinking is disabled for
# that one request (extra_body={"enable_thinking": False}, see
# _should_suppress_thinking/call_llm) when ALL of the previous turn's
# tool_calls were auto-approved (same per-tier policy as has_tool_calls,
# see approval_policy.py); normal thinking stays active for a task's
# first turn or as soon as a sensitive tool is involved, where reasoning
# has the most value. Migrated off an earlier `/no_think` text prefix,
# confirmed to have no effect on this backend (docs/resolved-bugs.md
# #entries at the time) onto this real per-request parameter — verified
# against the actual downloaded Qwen3.8 files before writing this
# (docs/briefs/qwen3.8-27b-evaluation.md, Phase 1).
ADAPTIVE_THINKING = os.environ.get("ADAPTIVE_THINKING", "false").lower() == "true"

# Independent mechanism from ADAPTIVE_THINKING above — caps HOW DEEP
# reasoning goes on every call (extra_body={"reasoning_effort": ...}, a
# real per-request TabbyAPI parameter, bare top-level key — same wire
# convention as enable_thinking, confirmed empirically against production
# TabbyAPI/Qwen3.8 rather than assumed from the model card's own Python
# example, which shows it as a separate SDK kwarg and could have implied
# a different wire shape; docs/engineering-log.md, "reasoning_effort
# tuning, Phase 0"), unconditionally — no turn-based gate like
# ADAPTIVE_THINKING's approval-tier condition. Built after
# ADAPTIVE_THINKING=true's own campaign showed full suppression breaks
# long-horizon tasks that need to notice a dead end and self-correct
# (docs/engineering-log.md, "ADAPTIVE_THINKING=true campaign": a frozen
# 18-call navigate loop on T10, an unfinished wide search on A1) — this
# keeps SOME reasoning on every turn instead of an all-or-nothing cut.
# Empty by default (no override, the model's own "xhigh" default
# applies, byte-for-byte unchanged behavior). Any other value is a
# startup-time config error: an invalid reasoning_effort makes EVERY
# call fail (TabbyAPI's chat template raises on it, confirmed in the
# same Phase 0 probe), so failing loudly at import time beats a silent
# per-request 400 discovered mid-campaign.
REASONING_EFFORT = os.environ.get("REASONING_EFFORT", "")
if REASONING_EFFORT and REASONING_EFFORT not in ("xhigh", "medium", "low"):
    raise ValueError(
        f"REASONING_EFFORT={REASONING_EFFORT!r} is not one of 'xhigh', 'medium', 'low' (or empty for no override)"
    )

# DOCUMENTED file-consumption path (Phase 1d-revised, see docs/history.md,
# T5): a download triggered in the browser lands in a volume now shared
# read-only with the filesystem MCP server (see docker-compose.yml,
# --output-dir/agent-downloads), under /downloads — never in the
# playwright-mcp container's own filesystem (fetch()/browser_evaluate as
# a file-transfer channel was explicitly ruled out, see docs/history.md:
# that's not a read tool's primitive). Giving the real path rather than
# letting the model guess one (observed: /app/.playwright-mcp/,
# /.playwright-mcp/ — both wrong) is direct anti-fabrication for this
# case. Directive text kept in French (sent to the model, behavior not
# documentation — CLAUDE.md rule #11).
DOWNLOAD_DIRECTIVE = (
    "Pour un fichier à télécharger (lien/bouton de téléchargement) : "
    "déclenche le téléchargement dans le navigateur, puis lis son contenu "
    "via l'outil filesystem read_file sous /downloads/<nom_du_fichier> — "
    "jamais via browser_navigate/browser_evaluate vers un chemin du "
    "navigateur, que tu ne peux pas connaître à l'avance."
)

# Bulk verification (found while investigating T1, see docs/history.md):
# the real blocker was neither a request format nor an outage, but an
# insufficient iteration budget facing information visible ONLY on
# detail pages (never the listing), potentially forcing as many
# navigations as candidate items — the model would end up guessing a URL
# (rightly blocked by the anti-fabrication guardrail), proof it had
# identified the right problem without the right solution. First fixed
# via browser_evaluate (JS code written by the model,
# TIER_SENSITIVE/NEVER_GRANTABLE); browser_extract now accepts a `urls`
# parameter (bulk mode, mcp-client) that does the same thing in
# TIER_READ, without depending on the model to write correct JS every
# time — instruction updated accordingly. Directive text kept in French
# (behavior, CLAUDE.md rule #11).
BULK_CHECK_DIRECTIVE = (
    "Si l'information cherchée (référence, prix...) n'apparaît PAS sur la "
    "page de listing/index mais seulement sur la page de détail de chaque "
    "élément, et qu'il faudrait en vérifier PLUSIEURS pour la trouver : "
    "n'ouvre pas ces pages une par une avec browser_navigate (budget "
    "d'itérations limité) — utilise browser_extract avec le paramètre "
    "urls (liste des URL candidates) pour vérifier TOUTES les pages en UN "
    "seul appel."
)

# Temporal awareness (PLAN.md Phase 1, point 7 — a dedicated amendment,
# never implemented until this fix despite T11 already being present in
# the harness since Phase 0, see docs/history.md: confirmed via an
# exhaustive grep during the T11 diagnosis). Triggered by the T11 probe
# itself: `browser_extract(query="Python 3.13")` — the model does
# navigate to python.org (the "first hop" fix) but queries the page with
# a version prefix drawn from ITS OWN frozen knowledge, missing the
# version actually displayed (3.14.x).
#
# Cutoff date: NO official date published in the local model card, true
# of both models served under this directive so far — checked again
# against the current model (models/qwen3.8-27b-exl3-4.50bpw/README.md):
# no "knowledge cutoff" mention, same gap as the original Qwen3.6 card
# this directive was designed against (which also placed its release
# after "the February release of the Qwen3.5 series" and cited AIME 2026
# issues in its benchmarks — Qwen3.8's card makes no equivalent claims to
# re-derive a bound from). The empirical grounding for the CONSERVATIVE
# bound below (Python 3.13 claimed as the latest version when 3.14
# already exists, i.e. actual knowledge older than the announced release
# date) was observed on Qwen3.6 via the T11 probe (docs/history.md) and
# has NOT been re-run on Qwen3.8 — kept as the safe default pending that
# re-verification, not because it was re-confirmed.
# Query-wording bias (found AFTER the first version of this directive,
# see docs/history.md — T11 probe: the model does decide to check, "My
# knowledge might be outdated", BUT then queries browser_extract with
# "Python 3.13" — its own assumed value injected into the search query
# itself — instead of a neutral term, and so retrieves the old version
# still present elsewhere on the page (release history). Checking via
# the web is pointless if the verification query is already biased by
# the assumed answer.
PEREMPTION_DIRECTIVE = (
    "\nTes connaissances ont une date de coupure antérieure à aujourd'hui "
    "— probablement plus ancienne que tu ne le penses (déjà observé : tu "
    "as annoncé Python 3.13 comme dernière version alors que 3.14 existe "
    "déjà). Avant d'affirmer un fait volatil (version d'un logiciel, prix, "
    "actualité, titulaire d'un rôle/poste, état d'un service en ligne), "
    "VÉRIFIE-le via le web plutôt que de répondre depuis ta mémoire — "
    "réserve la réponse de mémoire aux faits stables (histoire, "
    "mathématiques, définitions, documentation figée). En vérifiant, "
    "n'injecte JAMAIS dans ta requête de recherche/extraction une valeur "
    "précise que tu supposes déjà (ex. un numéro de version) — une page "
    "réelle mentionne souvent aussi d'anciennes valeurs (historique des "
    "versions), ta requête biaisée les retrouverait et te confirmerait à "
    "tort ton biais. Cherche plutôt un terme neutre décrivant ce que tu "
    "cherches (« dernière version stable », « version actuelle »)."
)

# Agent timezone (PLAN.md Phase 1, point 7a): from the host env (TZ, see
# docker-compose.yml), default Europe/Paris (this deployment's timezone,
# verified via `timedatectl` on the host — Docker containers do NOT
# automatically inherit the host's timezone, they run in UTC by default
# without this explicit setting).
_AGENT_TIMEZONE = os.environ.get("TZ", "Europe/Paris")
_WEEKDAYS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
_MONTHS_FR = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]


def _date_directive() -> str:
    """
    Date injection (PLAN.md Phase 1, point 7a): DAY granularity ONLY,
    never the time — preserves the ExLlamaV3 prefix cache (see
    docs/history.md, "chasing cache=0"): a value that only changes once a
    day, not on every turn or every second. Placed last in the static
    system block (after DOWNLOAD_DIRECTIVE/BULK_CHECK_DIRECTIVE/
    PEREMPTION_DIRECTIVE, before _merged_plan_directive's per-turn
    content, which is more volatile) — maximizes the length of the prefix
    that's actually stable from one turn to the next within the same day.
    """
    now = datetime.now(zoneinfo.ZoneInfo(_AGENT_TIMEZONE))
    return f"\nDate actuelle : {_WEEKDAYS_FR[now.weekday()]} {now.day} {_MONTHS_FR[now.month - 1]} {now.year} ({_AGENT_TIMEZONE})."


# Safety net (a real bug observed in real usage with llama-server — the
# turboquant-webp fork — on the task "go to wikipedia.org and search for
# the article about the city of Toulouse", see README, bug table): a
# model can end a turn WITH NO structured tool_calls AND no visible
# answer text.
#
# Root cause confirmed by reading the fork's parser
# (common/chat-auto-parser-generator.cpp): reasoning (<think>...) is
# captured as FREE text, NOT constrained by the grammar, until the
# closing </think> tag is encountered — the strict tool-calling grammar
# is only applied AFTER that tag. If the model "attempts" a tool call in
# prose (e.g. the <tool_call><function=...> syntax it has seen rendered
# by the template for its own previous turns) WITHOUT having closed
# </think> beforehand — typically after abnormally long/repetitive
# reasoning, akin to the semantic drift already documented for Ollama —
# this attempt stays trapped in the unconstrained zone and is never
# recognized as a real OpenAI tool_calls. Also confirmed NON-deterministic
# (replaying the SAME prompt sometimes gives a correct tool_calls,
# sometimes this failure) and confirmed fixed by ADAPTIVE_THINKING/no_think
# (which entirely avoids this vulnerable code path, see above) — but
# /no_think only gets injected starting from the turn FOLLOWING an
# auto-approved turn, not on a task's very first turn, which is exactly
# where the bug was observed.
#
# Two complementary mitigations, neither fixing the cause on the
# model/server side (out of scope here):
#   1. has_tool_calls automatically loops back to call_llm up to
#      MAX_EMPTY_ANSWER_RETRIES times before giving up (see this function
#      below) — cumulative budget for the whole task, like
#      tool_iterations, not reset to zero on every attempt.
#   2. _extract_fallback_tool_call (see below): before even counting this
#      turn as a failure, attempts to extract a <tool_call> trapped in
#      the text and reconstruct it into structured tool_calls — when
#      that succeeds, the turn continues normally (approval,
#      execution...) without ever consuming a retry or displaying the
#      fallback notice.
# Beyond both, app/main.py displays an explicit notice
# (_format_empty_answer_notice) rather than leaving the conversation
# silent.
MAX_EMPTY_ANSWER_RETRIES = int(os.environ.get("MAX_EMPTY_ANSWER_RETRIES", "1"))

# Fixed per-image token allowance in the context-composition estimate
# (see describe_context/POST /context, services/dashboard): an exact
# count would depend on the served model's visual tokenizer (out of
# scope here, see README, Out of scope) — a constant is enough for an
# order of magnitude shown on the observability dashboard.
IMAGE_TOKEN_ESTIMATE = int(os.environ.get("IMAGE_TOKEN_ESTIMATE", "1500"))


def estimate_tokens(text: str) -> int:
    """
    Rough estimate (~3.5 characters/token, order of magnitude for mixed
    English/French), not an exact tokenizer — used only by POST /context
    for the observability dashboard (services/dashboard), which shows
    trends rather than exact counts (see README, Out of scope: an exact
    tokenizer is explicitly ruled out).
    """
    if not text:
        return 0
    return math.ceil(len(text) / 3.5)

# Recognizes a tool call written in prose using Qwen's XML-ish format
# (<tool_call><function=NAME><parameter=KEY>VALUE</parameter>...</function>
# </tool_call>), as observed trapped in reasoning_content in real usage.
# DOTALL to capture multi-line parameter values (e.g. text to type
# containing a line break).
_FALLBACK_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*<function=([a-zA-Z0-9_]+)>(.*?)</function>\s*</tool_call>", re.DOTALL
)
_FALLBACK_PARAMETER_RE = re.compile(
    r"<parameter=([a-zA-Z0-9_]+)>(.*?)</parameter>", re.DOTALL
)


def _extract_fallback_tool_call(content: str) -> Optional[dict]:
    """
    Attempts to extract a valid tool_call from text (reasoning or
    content) when the model wrote one in prose instead of having it
    recognized by the server's grammar (see the MAX_EMPTY_ANSWER_RETRIES
    comment above for the root cause). Best-effort: a single call
    recognized per turn (the first one found), no validation against the
    tool's JSON schema — call_tools/mcp-client will fail cleanly if the
    extracted arguments are wrong, same as for a normally structured
    tool_call. Returns None if nothing recognizable is found.
    """
    match = _FALLBACK_TOOL_CALL_RE.search(content or "")
    if not match:
        return None
    tool_name = match.group(1)
    params_blob = match.group(2)
    arguments = {
        key: value.strip() for key, value in _FALLBACK_PARAMETER_RE.findall(params_blob)
    }
    return {"name": tool_name, "args": arguments, "id": f"fallback_{uuid.uuid4().hex[:12]}"}

# The trailing "?" makes the closing tag optional: covers both content
# already persisted by call_llm (always closed before returning) and
# text still being streamed on the app/main.py side (potentially not
# closed yet at test time).
_THINK_BLOCK_RE = re.compile(r"<think>.*?(</think>|\Z)", re.DOTALL)


def has_visible_answer(content: str) -> bool:
    """Is there any text left outside a <think> tag? Used by
    has_tool_calls (automatic retry) and app/main.py (empty-answer
    notice)."""
    return bool(_THINK_BLOCK_RE.sub("", content or "").strip())


# Merged-planning mode (PLANNING_MODE="merged", effort 2 point 3, see
# docs/briefs/update-plan.md "2.1 addendum"): a non-MCP, graph-only
# synthetic tool — dispatched locally in _execute_tool_calls, never sent
# to mcp-client. Only exposed by _get_bound_llm when PLANNING_MODE ==
# "merged". Two actions, deliberately no third "fail"/"replan" action: a
# stuck subtask is handled by calling set_plan again (replacing the
# remaining subtasks) rather than by ever persisting an "echoue" status —
# that status was what used to route to the now-removed replan_task node
# in the 4-flag architecture (docs/resolved-bugs.md #61), exactly the
# auxiliary call this mode was built to avoid.
_MANAGE_PLAN_TOOL_NAME = approval_policy.MANAGE_PLAN_TOOL_NAME
_MANAGE_PLAN_TOOL = {
    "type": "function",
    "function": {
        "name": _MANAGE_PLAN_TOOL_NAME,
        "description": (
            "Gère ton plan de sous-tâches directement (mode planification "
            "fusionnée) : n'appelle aucun autre outil le même tour. "
            "`set_plan` : crée le plan initial (premier appel) ou remplace "
            "les sous-tâches restantes (si une sous-tâche bloque) — 2 à 12 "
            "sous-tâches, chacune avec description et critère de succès. "
            "`complete_subtask` : marque la sous-tâche `subtask_index` "
            "comme atteinte et passe à la suivante."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["set_plan", "complete_subtask"]},
                "subtasks": {
                    "type": "array",
                    "description": "Requis pour set_plan.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "success_criterion": {"type": "string"},
                        },
                        "required": ["description", "success_criterion"],
                    },
                },
                "subtask_index": {"type": "integer", "description": "Requis pour complete_subtask."},
            },
            "required": ["action"],
        },
    },
}


# Planner JSON parsing (used by revise_plan, see _validate_plan_json
# below): recognizes a possible ```json ... ``` / ``` ... ``` wrapper
# around the reply — the model may wrap the JSON despite the raw-output
# instruction, as already observed for other output formats in this file
# (see _extract_fallback_tool_call above).
_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n?(.*?)\n?```$", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    match = _CODE_FENCE_RE.match(text.strip())
    return match.group(1).strip() if match else text.strip()


class PlanValidationError(ValueError):
    """Raised by _validate_plan_json: the planner's reply is unusable."""


_PLAN_SUBTASKS_MIN = 1
_PLAN_SUBTASKS_MAX = 8


def _validate_plan_json(raw: str) -> list[dict]:
    """
    Schema validated PROGRAMMATICALLY (Iteration 1): strips
    <think>...</think> then a possible fence wrapper, requires
    {"sous_taches": [{"description":..., "critere_succes":..., "outils":
    [...]}, ...]}, 1 to 8 items, non-empty description/criterion. `outils`
    optional on the LLM side (falls back to an empty list) — provides a
    concrete basis for the validation pipeline (Iteration 3,
    app/plan_validation.py: existence/tier of the declared tools),
    without which a purely editorial subtask (e.g. "write up the final
    answer") would have no valid representation. Raises
    PlanValidationError with an explicit reason otherwise — never a
    partially built plan from an invalid reply.
    """
    text = _strip_code_fence(_THINK_BLOCK_RE.sub("", raw or ""))
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlanValidationError(f"invalid JSON: {exc}") from exc
    subtasks = data.get("sous_taches") if isinstance(data, dict) else None
    if not isinstance(subtasks, list):
        raise PlanValidationError("'sous_taches' key (list) missing or invalid")
    if not (_PLAN_SUBTASKS_MIN <= len(subtasks) <= _PLAN_SUBTASKS_MAX):
        raise PlanValidationError(
            f"number of subtasks out of bounds ({len(subtasks)}, expected "
            f"{_PLAN_SUBTASKS_MIN}-{_PLAN_SUBTASKS_MAX})"
        )
    validated = []
    for i, item in enumerate(subtasks):
        if not isinstance(item, dict):
            raise PlanValidationError(f"subtask {i} is not a JSON object")
        description = item.get("description")
        critere = item.get("critere_succes")
        if not isinstance(description, str) or not description.strip():
            raise PlanValidationError(f"subtask {i}: description missing or empty")
        if not isinstance(critere, str) or not critere.strip():
            raise PlanValidationError(f"subtask {i}: critere_succes missing or empty")
        outils = item.get("outils")
        if outils is None:
            outils = []
        if not isinstance(outils, list) or not all(isinstance(t, str) for t in outils):
            raise PlanValidationError(f"subtask {i}: outils must be a list of strings")
        validated.append(
            {
                "description": description.strip(),
                "success_criterion": critere.strip(),
                "tools": [t.strip() for t in outils if t.strip()],
            }
        )
    return validated


PLANNER_SYSTEM_PROMPT = (
    "Tu es le planificateur d'un agent qui accomplit des tâches web. À "
    "partir de l'objectif de l'utilisateur, décompose-le en 1 à 8 "
    "sous-tâches concrètes et vérifiables. Réponds UNIQUEMENT par un JSON "
    'de la forme {"sous_taches": [{"description": "...", "critere_succes": '
    '"...", "outils": ["nom_outil", ...]}, ...]}, rien d\'autre : pas de '
    "texte avant/après, pas de balise <think>, pas de bloc de code. "
    '"outils" liste les noms des outils que tu comptes utiliser pour cette '
    "sous-tâche (liste vide si aucun, ex. une sous-tâche purement "
    "rédactionnelle)."
)


async def _fetch_verification_snapshot(objective: str) -> str:
    """
    Capture a FRESH browser_snapshot at verification time — grounding fix
    found during the Iteration 4 live probe (see docs/history.md): the raw
    result of the last tool_call (e.g. a browser_click confirmation) is
    often TERSE, without the resulting page content. verify_action would
    then judge a subtask "failed" relying solely on success_criterion —
    sometimes itself poorly grounded (e.g. "use the search bar" on a site
    that has none) — without ever seeing that the actual page already
    showed valid progress (e.g. pagination). Best-effort: mcp-client error
    -> empty string, the verifier then judges with only the info already
    available (identical behavior to before this fix) — never a blocker
    for a side capture issue, same philosophy as the rest of this file.
    """
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            result, _ = await _call_mcp_tool(client, "browser_snapshot", {})
        truncated = _truncate_browser_result(result, BROWSER_TOOL_OUTPUT_MAX_CHARS, objective)
        blocks = truncated.get("content", [])
        texts = [b["text"] for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(texts)
    except Exception:
        logger.warning("Verification capture (browser_snapshot) unavailable, judging without it.", exc_info=True)
        return ""


async def _grounding_snapshot(state: dict, objective: str) -> Optional[str]:
    """
    Snapshot of the current page to ground a plan revision in what
    ACTUALLY exists (Iteration 4 grounding fix, see docs/history.md).
    `None` if no navigation has happened yet for this task
    (state["current_page_url"], Phase 1) — revise_plan, its sole caller,
    is only ever triggered after validate_plan has rejected a plan that
    already references a page, so this stays populated in practice.
    """
    if not state.get("current_page_url"):
        return None
    return await _fetch_verification_snapshot(objective) or None


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    tool_iterations: int
    approved: Optional[bool]
    # Consecutive auto-approved turns since the last pass through
    # require_approval (see AUTO_APPROVAL_STREAK_LIMIT above).
    auto_approval_streak: int
    # Number of Open WebUI messages (user/assistant roles) already merged
    # into this thread — lets app/main.py submit only the NEW messages on
    # each turn instead of the full history Open WebUI resends (already
    # persisted here via the checkpointer), avoiding duplicating it in
    # "messages" on every turn.
    owui_message_count: int
    # State of the <think> tag (see _think_state above), carried over from
    # one call_llm invocation to the next within the same user turn —
    # needed since AUTO_APPROVED_TOOLS, which lets call_llm run several
    # times in a row with no approval pause in between. Without this
    # carry-over, each iteration would reopen its own <think> tag, and
    # Open WebUI only renders the very first one as a collapsible bubble:
    # later ones showed up as raw visible text. Reset to False on each new
    # turn (see _resolve_run, app/main.py), like tool_iterations.
    think_opened: bool
    think_closed: bool
    # Session grants (Phase 3): tool names a human has approved "for the
    # session" via require_approval (see that node below) rather than just
    # once. A tool in this list is capped at TIER_REVERSIBLE (auto +
    # audit) for the rest of the thread, even if it would normally be
    # TIER_SENSITIVE (see approval_policy.effective_tier). Lives in graph
    # state, hence in the MemorySaver checkpointer (in-memory only): a
    # service restart loses the grants along with the rest of the thread —
    # intended behavior, not a bug (see README, Human supervision section).
    session_grants: list
    # Transient decision paired with "approved" (see require_approval):
    # True if the human answered "approve for the session" rather than
    # just "approve". Consumed then reset to False as soon as
    # require_approval has applied the grant, so as not to re-trigger a
    # grant on every later resumption of the thread.
    grant_session: bool
    # Retry counter for the "empty answer" safety net (see
    # MAX_EMPTY_ANSWER_RETRIES above) — cumulative budget for the whole
    # task, like tool_iterations, never reset between retries.
    empty_answer_retries: int
    # Explicit signal (not inferred from message shape, too fragile — a
    # normal LLM turn that analyzed an image via vision also produces an
    # AIMessage right after an image message): True only when the last
    # message came from run_slash_command_direct AND carried an image, so
    # that main.py knows to reconstruct the image display for THIS turn
    # (_render_visible_answer) without persisting it as base64 in the
    # assistant message itself. call_llm resets it to False on every
    # call: it's the only other node that ends a turn on a visible
    # AIMessage, hence the only reset needed for this signal to stay
    # correct regardless of how this turn ends.
    slash_command_image_shown: bool
    # URL-fabrication guardrail (Phase 1, see _check_navigate_url): set of
    # URLs "seen" for this task — starting target (scope roots, extracted
    # from the 1st human message), navigations already executed, and
    # links observed in the content returned by a browser_* tool
    # (snapshot/DOM). Reset on every new user turn (see run_input,
    # app/main.py), like tool_iterations — the scope is THIS TASK's, not
    # the whole conversation.
    observed_urls: list
    # URL of the page currently loaded in the browser (last "Page URL: ..."
    # value seen in a browser_* tool result), needed to resolve RELATIVE
    # links (e.g. "/catalog/product-14.html") to absolute URLs before
    # adding them to observed_urls.
    current_page_url: Optional[str]
    # Links of the LAST page seen (replaced, not accumulated, unlike
    # observed_urls): used to steer the model toward real links when a
    # fabricated navigation is rejected (see _execute_tool_calls) — "here's
    # where you actually are", not the whole navigation history which
    # would be less actionable.
    current_page_links: list
    # Counter of navigation attempts to an unobserved URL, blocked BEFORE
    # execution (see _check_navigate_url) — Phase 1 metric, not just a
    # silent brake.
    fabricated_navigation_attempts: int
    # Explicit task plan (Iteration 1, Phase 1 "cognitive core" — see
    # docs/briefs/archives/coeur-cognitif.md): list of {description,
    # success_criterion, status, attempts, result}. status ∈ {"a_faire",
    # "en_cours", "fait", "echoue"} (free string, no dedicated enum —
    # consistent with failure_cause in the test harness). Currently only
    # ever populated by PLANNING_MODE="merged"'s manage_plan tool (see
    # _execute_tool_calls) — the planner node that used to populate it in
    # "nodes" mode was removed (docs/resolved-bugs.md #61), validate_plan/
    # revise_plan/require_plan_approval below stay wired as a safety net
    # for however else a plan might get set. Reset to [] on every NEW
    # top-level user message (see run_input, app/main.py), like
    # observed_urls.
    plan: list
    # Episode compaction (Phase 2, PLAN.md): subtask_message_start[i] is
    # len(messages) at the moment plan[i] became "en_cours" — lets
    # _apply_episode_compaction find each completed subtask's raw message
    # range without scanning message content. Set by revise_plan (fresh
    # list, index 0) or manage_plan's set_plan action (see
    # _execute_tool_calls). Reset to [] on every new top-level user
    # message (see run_input, app/main.py), same lifecycle as plan.
    subtask_message_start: list
    # Plan validation pipeline (Iteration 3, see validate_plan/
    # revise_plan/require_plan_approval below). plan_validation_reasons:
    # reasons for the LAST rejection, [] if the current plan is valid (or
    # not yet evaluated). plan_validation_cycles: number of rejections
    # suffered for THIS task, capped by PLAN_VALIDATION_CYCLES_MAX, beyond
    # which human escalation kicks in rather than looping indefinitely.
    # Both reset to zero/empty on every new top-level user message (see
    # run_input, app/main.py).
    plan_validation_reasons: list
    plan_validation_cycles: int
    # Plan approval (Iteration 3): mirrors approved/grant_session
    # (require_approval) but for the WHOLE plan rather than one tool_call —
    # see require_plan_approval. plan_grant: persisted (unlike
    # plan_grant_session, transient) — a plan-level grant, once given,
    # avoids the pause on a later replan WITHIN THE SAME TASK as long as
    # the new tier stays TIER_REVERSIBLE or below, never for
    # TIER_SENSITIVE (same philosophy as NEVER_GRANTABLE_TOOLS,
    # approval_policy.py).
    plan_approved: Optional[bool]
    plan_grant_session: bool
    plan_grant: bool


# Token cap per TURN (a single LLM call), not for the whole conversation:
# without it, a repetition-loop drift (observed in real usage with a
# heavily quantized model — see README) generates until it saturates the
# whole context before stopping (tens of seconds, thousands of tokens),
# without ever producing tool_calls or tripping our own guardrails
# (MAX_TOOL_ITERATIONS/AUTO_APPROVAL_STREAK_LIMIT), which only count tool
# iterations, not generation length.
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "2048"))

llm = ChatOpenAI(
    base_url=LLM_BASE_URL,
    api_key="not-needed",       # tabbyapi (disable_auth: true)/llama-server/Ollama don't check the key by default
    model="agent-llm",          # must match model_name in services/tabbyapi/config.yml
    temperature=0.2,
    max_tokens=LLM_MAX_TOKENS,
)

# Bug discovered under real conditions while verifying the Iteration 3
# live campaign (see docs/history.md): the auxiliary planner LLM calls
# used `llm` above, capped at LLM_MAX_TOKENS (2048, sized for the main
# conversational turn).
# TabbyAPI reasons in a reasoning_content field SEPARATE from
# content before answering (confirmed via a direct non-streaming call to
# TabbyAPI, originally on Qwen3.6 and re-confirmed on Qwen3.8 during the
# Phase 1 migration, docs/briefs/qwen3.8-27b-evaluation.md); this reasoning, often long, consumed the whole budget on its
# own, truncating `content` to empty or mid-JSON (finish_reason="length")
# — every validator then systematically fell back to its error path,
# never a real evaluation. `/no_think` as a prompt prefix — ADAPTIVE_THINKING's
# mechanism at the time — did NOT suppress reasoning on this backend
# (verified by the same direct call; ADAPTIVE_THINKING has since migrated
# to the same real per-request parameter used below, see
# docs/briefs/qwen3.8-27b-evaluation.md, Phase 1) — solution adopted here:
# a more generous token budget, dedicated to these structured calls,
# separate from the main loop's budget (whose small value remains an
# intentional safety net against repetition drift, see LLM_MAX_TOKENS).
PLANNER_MAX_TOKENS = int(os.environ.get("PLANNER_MAX_TOKENS", "8192"))
# Thinking curbed on auxiliary calls (revise_plan, via planner_llm) —
# TabbyAPI exposes a real
# PER-REQUEST server-side parameter (`GET /openapi.json`,
# ChatCompletionRequest schema: `enable_thinking: bool`), verified LIVE
# before writing this fix (real call with a JSON planning prompt, see
# docs/history.md): `reasoning_content: null`, immediate valid JSON, no
# reasoning. `extra_body` is a native langchain-openai parameter
# (verified: `"extra_body" in inspect.signature(ChatOpenAI).parameters`).
# Fixed here at client construction (planner_llm never varies); the main
# loop's ADAPTIVE_THINKING uses the same parameter but bound per-call
# instead, since it's conditional turn-to-turn (see
# _should_suppress_thinking) — it originally used a `/no_think` text
# prefix, confirmed ineffective on this backend by the same direct call
# that motivated this fix, migrated since (docs/briefs/
# qwen3.8-27b-evaluation.md, Phase 1).
# PLANNER_THINKING_ENABLED (default false = thinking curbed) rather than a
# hardcoded disable: allows a rollback with no code redeploy if plan/
# judge quality were to degrade in practice.
PLANNER_THINKING_ENABLED = os.environ.get("PLANNER_THINKING_ENABLED", "false").lower() == "true"
planner_llm = ChatOpenAI(
    base_url=LLM_BASE_URL,
    api_key="not-needed",
    model="agent-llm",
    temperature=0.2,
    max_tokens=PLANNER_MAX_TOKENS,
    extra_body={"enable_thinking": PLANNER_THINKING_ENABLED},
)

# Schema of the MCP tools (filesystem/browser), fetched from mcp-client
# and cached for the process's lifetime. Without
# this bind_tools, the LLM has no knowledge that these tools exist and can
# therefore never produce tool_calls, whatever model is served —
# has_tool_calls()/require_approval() then stay dead code.
_tools_schema_cache: Optional[list] = None


def _visual_navigation_filter(tool_name: str) -> bool:
    """True if `tool_name` stays in the schema under the current
    VISUAL_NAVIGATION_ONLY setting — the single filter every consumer of
    _get_tools_schema (bind_tools, _route_entry, merged-planning's
    known_tools) sees, so a plan or a slash command can't reference a
    tool the model itself was never shown. Off (default): hides the
    vision-only coordinate tools instead, to keep the default schema's
    weight exactly as it was before this mode existed."""
    if VISUAL_NAVIGATION_ONLY:
        return tool_name not in _VISUAL_ONLY_BLOCKED_TOOLS
    return tool_name not in _VISION_ONLY_TOOLS


async def _get_tools_schema() -> list:
    """Fills/returns _tools_schema_cache — factored out of _get_bound_llm
    so it's also usable by _route_entry (validating a slash command's tool
    name) without an extra HTTP request once cached. Filtered once here
    (VISUAL_NAVIGATION_ONLY is a static env var, not a per-request
    choice), not re-filtered on every call."""
    global _tools_schema_cache
    if _tools_schema_cache is None:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{MCP_CLIENT_URL}/tools/schema")
                resp.raise_for_status()
                tools = resp.json().get("tools", [])
                _tools_schema_cache = [
                    t for t in tools if _visual_navigation_filter(t.get("function", {}).get("name"))
                ]
        except (httpx.HTTPError, ValueError):
            # mcp-client unreachable or invalid response: degrade with no
            # tools rather than failing the whole conversation.
            _tools_schema_cache = []
    return _tools_schema_cache


async def _get_bound_llm() -> ChatOpenAI:
    schema = await _get_tools_schema()
    # Synthetic, non-MCP tool, see PLANNING_MODE above.
    extra_tools = [_MANAGE_PLAN_TOOL] if PLANNING_MODE == "merged" else []
    if not schema:
        return llm.bind_tools(extra_tools) if extra_tools else llm
    # extra_tools FIRST (correction 2/2, fifth-condition diagnostic, see
    # docs/history.md "EFFORT 2" point 3): manage_plan previously sat
    # last, after the full ~63-64 MCP/browser catalog — the one variable
    # left untried after cause 3's fix (persistent plan section) still
    # measured merged_plan_calls=0. No-op outside merged mode
    # (extra_tools == [], list identity unchanged).
    return llm.bind_tools(extra_tools + schema)


async def retrieve_context(state: AgentState) -> dict:
    last_user_msg = next(
        (m.content for m in reversed(state["messages"]) if m.type == "human"), ""
    )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{CONTEXT_MANAGER_URL}/retrieve", json={"query": last_user_msg, "top_k": 5}
            )
            resp.raise_for_status()
            snippets = resp.json().get("results", [])
    except httpx.HTTPError:
        snippets = []

    if not snippets:
        return {"messages": []}

    context_text = "\n".join(f"- {s}" for s in snippets)
    return {"messages": [{"role": "system", "content": f"Contexte pertinent récupéré :\n{context_text}"}]}


async def select_skill(state: AgentState) -> dict:
    last_user_msg = next(
        (m.content for m in reversed(state["messages"]) if m.type == "human"), ""
    )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{SKILL_MANAGER_URL}/match", json={"query": last_user_msg}
            )
            resp.raise_for_status()
            skill = resp.json().get("skill")
    except httpx.HTTPError:
        skill = None

    if not skill:
        return {"messages": []}

    return {"messages": [{"role": "system", "content": f"Skill activée : {skill['name']}\n{skill['content']}"}]}


async def _available_tools_hint() -> str:
    """
    Real list of available MCP tools (discovered under real conditions
    during the Iteration 3 live campaign, see docs/history.md): without it,
    the planner invents plausible but nonexistent tool names (e.g.
    "web_browser", "search") — systematically rejected by the heuristics
    (existing referenced tools, app/plan_validation.py), no plan would
    ever pass validation. Added to the USER message (not the system
    prompt, which is frozen) to stay up to date if the tool schema changes
    between tasks. Used by revise_plan.
    """
    schema = await _get_tools_schema()
    names = sorted({t.get("function", {}).get("name") for t in schema} - {None})
    if not names:
        return ""
    return (
        "\n\nOutils réellement disponibles (utilise UNIQUEMENT ces noms exacts "
        'dans "outils", liste vide si aucun ne s\'applique) : ' + ", ".join(names)
    )


def _plan_tier(plan: list) -> str:
    """
    Plan tier = worst tier among ALL tools declared by its subtasks
    (Iteration 3) — approval_policy.tool_tier(), which already falls back
    to TIER_SENSITIVE for an unknown tool (existing default "unknown tool
    = always sensitive", consistent here). No tool declared anywhere ->
    TIER_READ (nothing to approve upfront).
    """
    tiers = {approval_policy.tool_tier(tool) for subtask in plan for tool in subtask.get("tools", [])}
    if approval_policy.TIER_SENSITIVE in tiers:
        return approval_policy.TIER_SENSITIVE
    if approval_policy.TIER_REVERSIBLE in tiers:
        return approval_policy.TIER_REVERSIBLE
    return approval_policy.TIER_READ


async def validate_plan(state: AgentState, config: dict) -> dict:
    """
    Plan validation pipeline (Iteration 3, Phase 1 "cognitive core") —
    kept as a safety-value exception after the rest of the cognitive core
    was removed (see the flag block above). No-op (`{"messages": []}`) if
    PLAN_VALIDATION_ENABLED is disabled (default true) or if
    `state["plan"]` is empty. Otherwise: programmatic heuristics only
    (app/plan_validation.py, free — the LLM judge branch was removed
    along with the rest of the cognitive core, see docs/resolved-bugs.md
    #60). Rejection -> plan_validation_cycles incremented, reasons
    returned for route_after_validation.

    Logs a role="plan_validation" audit entry (coverage counter,
    docs/history.md EFFORT 2 "judge validity check"): heuristic rejection
    kept as its own signal rather than folded into a boolean, so a future
    reader of the audit log doesn't have to guess whether this ever
    fires.
    """
    if not PLAN_VALIDATION_ENABLED:
        return {"messages": []}
    plan = state.get("plan") or []
    if not plan:
        return {"messages": []}

    schema = await _get_tools_schema()
    known_tools = {t.get("function", {}).get("name") for t in schema}
    known_tools.discard(None)
    task_scope = _task_scope_urls(state["messages"])
    heuristic_reasons = plan_validation.validate_plan_heuristics(
        plan, known_tools=known_tools, task_scope_urls=task_scope
    )

    reasons = heuristic_reasons
    thread_id = config.get("configurable", {}).get("thread_id", "")
    audit_log.log_message(
        thread_id,
        "plan_validation",
        {"heuristic_rejected": bool(heuristic_reasons)},
    )

    if reasons:
        cycles = state.get("plan_validation_cycles", 0) + 1
        logger.warning("Plan rejected by validation (cycle %d): %s", cycles, reasons)
        # plan_approved reset to None HERE (not in require_plan_approval,
        # see its comment): whether this rejection leads to a revision or
        # a human escalation, any previous decision on an EARLIER plan
        # must never be reused for this one.
        return {"plan_validation_reasons": reasons, "plan_validation_cycles": cycles, "plan_approved": None}

    logger.info("Plan validated (%d subtask(s)).", len(plan))
    return {"plan_validation_reasons": [], "plan_approved": None}


def route_after_validation(state: AgentState) -> str:
    """
    Routing after validate_plan. PLAN_VALIDATION_ENABLED disabled ->
    "call_llm" (same flow as before this iteration). Rejected ->
    "revise_plan" as long as PLAN_VALIDATION_CYCLES_MAX isn't exceeded,
    otherwise "require_plan_approval" (human escalation, reasons
    displayed). Accepted -> "call_llm" if TIER_READ, or if TIER_REVERSIBLE
    and a plan grant is already given for this task (plan_grant, never
    for TIER_SENSITIVE), otherwise "require_plan_approval" (normal
    approval).
    """
    if not PLAN_VALIDATION_ENABLED:
        return "call_llm"
    reasons = state.get("plan_validation_reasons") or []
    if reasons:
        cycles = state.get("plan_validation_cycles", 0)
        return "revise_plan" if cycles <= PLAN_VALIDATION_CYCLES_MAX else "require_plan_approval"
    tier = _plan_tier(state.get("plan") or [])
    if tier == approval_policy.TIER_READ:
        return "call_llm"
    if tier == approval_policy.TIER_REVERSIBLE and state.get("plan_grant"):
        return "call_llm"
    return "require_plan_approval"


async def revise_plan(state: AgentState) -> dict:
    """
    Plan revision following a rejection by the validation pipeline
    (Iteration 3): nothing has been executed yet — the plan itself is
    judged structurally/semantically insufficient before the first turn.
    Regenerates the WHOLE plan (no "done" subtask to preserve) with the
    rejection reasons as context. Degrades to a single-subtask plan on
    generation failure (HTTP transport, invalid JSON), never blocks.
    """
    reasons = state.get("plan_validation_reasons") or []
    first_human = next((m for m in state["messages"] if getattr(m, "type", None) == "human"), None)
    objective = first_human.content if first_human and isinstance(first_human.content, str) else ""
    motifs = "\n".join(f"- {r}" for r in reasons) or "(motif non précisé)"
    page_snapshot = await _grounding_snapshot(state, objective)
    snapshot_hint = (
        f"\nÉtat actuel de la page (ce qui est RÉELLEMENT visible maintenant, base-toi dessus) :\n{page_snapshot}\n"
        "ATTENTION : cet état ne montre que ce qui existe RÉELLEMENT — ne "
        "confonds jamais un élément visible ici (ex. un autre produit, une "
        "autre référence) avec ce que l'objectif demande explicitement. Si "
        "l'élément exact demandé par l'objectif n'apparaît nulle part après "
        "une recherche raisonnable, le plan doit conclure à son absence, "
        "jamais lui substituer un élément différent trouvé sur la page.\n"
        if page_snapshot
        else ""
    )
    context = (
        f"Objectif original : {objective}\n"
        f"Ta précédente proposition de plan a été rejetée pour les raisons suivantes :\n{motifs}\n"
        f"{snapshot_hint}"
        "Propose un NOUVEAU plan qui corrige ces problèmes."
    )
    try:
        tools_hint = await _available_tools_hint()
        response = await planner_llm.ainvoke(
            [SystemMessage(content=PLANNER_SYSTEM_PROMPT), HumanMessage(content=context + tools_hint)]
        )
        subtasks = _validate_plan_json(response.content)
    except Exception:
        logger.warning("Plan revision failed, falling back to a single-subtask plan.", exc_info=True)
        subtasks = [{"description": objective, "success_criterion": "objectif de la tâche atteint", "tools": []}]

    plan = [{**st, "status": "a_faire", "attempts": 0, "result": None} for st in subtasks]
    if plan:
        plan[0]["status"] = "en_cours"
    logger.info("Revised plan (%d subtask(s), validation cycle): %s", len(plan), plan)
    return {"plan": plan, "subtask_message_start": [len(state["messages"])] if plan else []}


async def require_plan_approval(state: AgentState) -> dict:
    """
    Human approval of the PLAN (Iteration 3): mirrors require_approval but
    for the whole plan rather than a single tool_call — pauses
    (NodeInterrupt) while plan_approved is None. Stays NOT MERGEABLE with
    the individual approval of a TIER_SENSITIVE tool at execution time:
    this node is an ADDITIONAL gate upstream, require_approval/
    _execute_tool_calls stay unchanged and still apply regardless.
    """
    if state.get("plan_approved") is None:
        raise NodeInterrupt("Approbation humaine du plan requise avant exécution.")
    # DO NOT reset plan_approved to None here: route_after_plan_approval
    # (right after) still needs to read the decision (True/False) exactly
    # as this node just received it — same pitfall already avoided by
    # require_approval, which leaves "approved" intact for
    # route_after_approval and only resets it elsewhere
    # (_execute_tool_calls, for the next turn). Here, it's validate_plan
    # that resets plan_approved to None on every newly proposed plan (see
    # that node).
    updates = {"plan_grant_session": False}
    if state.get("plan_grant_session"):
        updates["plan_grant"] = True
    return updates


def route_after_plan_approval(state: AgentState) -> str:
    return "call_llm" if state["plan_approved"] else "reject_plan"


async def reject_plan(state: AgentState) -> dict:
    """Mirrors reject_tools, plan-side: the human rejected the proposed plan, the task stops here."""
    return {"messages": [{"role": "assistant", "content": "Plan refusé par l'utilisateur — tâche non exécutée."}]}


def _is_image_message(message) -> bool:
    return (
        getattr(message, "type", None) == "human"
        and isinstance(message.content, list)
        and any(isinstance(b, dict) and b.get("type") == "image_url" for b in message.content)
    )


_CONTEXT_BLOCK_SKELETON = (
    ("System prompt", "system"),
    ("Skills", "skills"),
    ("Schéma d'outils", "tools_schema"),
    ("Historique (texte)", "history_text"),
    ("Images", "images"),
)


def describe_context(messages: list, pending_text: Optional[str] = None) -> list[dict]:
    """
    Approximate breakdown (see estimate_tokens) of the context as it would
    be built for an LLM call (see call_llm), for use by POST /context
    (app/main.py) and hence the observability dashboard
    (services/dashboard) — never a real LLM call, and the tool schema is
    read as-is from _tools_schema_cache (never recomputed via
    _get_bound_llm, which would make an HTTP call to mcp-client: /context
    must stay strictly read-only, with no side effect, like /pending).

    Empty `messages` (thread unknown to the checkpointer) -> all blocks at
    zero rather than still including the transient system prompt
    (the transient directives below): nothing has been composed yet for
    this thread.
    """
    if not messages:
        return [
            {"label": label, "kind": kind, "est_tokens": 0, "count": 0}
            for label, kind in _CONTEXT_BLOCK_SKELETON
        ]

    system_parts = [DOWNLOAD_DIRECTIVE, BULK_CHECK_DIRECTIVE, PEREMPTION_DIRECTIVE]
    skills_parts = []
    history_parts = []
    image_count = 0

    for message in messages:
        content = message.content
        if getattr(message, "type", None) == "system":
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            if text.startswith("Skill activée :"):
                skills_parts.append(text)
            else:
                system_parts.append(text)
        elif _is_image_message(message):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "image_url":
                    image_count += 1
                elif block.get("type") == "text":
                    history_parts.append(block.get("text", ""))
        else:
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            history_parts.append(text)

    blocks = [
        {
            "label": "System prompt",
            "kind": "system",
            "est_tokens": estimate_tokens("\n".join(system_parts)),
            "count": len(system_parts),
        },
        {
            "label": "Skills",
            "kind": "skills",
            "est_tokens": estimate_tokens("\n".join(skills_parts)),
            "count": len(skills_parts),
        },
        {
            "label": "Schéma d'outils",
            "kind": "tools_schema",
            "est_tokens": estimate_tokens(json.dumps(_tools_schema_cache or [], ensure_ascii=False)),
            "count": len(_tools_schema_cache or []),
        },
        {
            "label": "Historique (texte)",
            "kind": "history_text",
            "est_tokens": estimate_tokens("\n".join(history_parts)),
            "count": len(history_parts),
        },
        {
            "label": "Images",
            "kind": "images",
            "est_tokens": image_count * IMAGE_TOKEN_ESTIMATE,
            "count": image_count,
        },
    ]
    if pending_text:
        blocks.append(
            {
                "label": "Approbation en attente",
                "kind": "pending",
                "est_tokens": estimate_tokens(pending_text),
                "count": 1,
            }
        )
    return blocks


def _apply_image_retention(messages: list) -> list:
    """
    Keeps only the last MAX_IMAGES_IN_CONTEXT image messages (see
    _is_image_message) in the list sent to the LLM; earlier ones are
    replaced by an indicative text message. Returns a NEW list (never an
    in-place mutation of the original messages, which are the same Python
    objects persisted by the checkpointer) — this is what guarantees this
    filtering stays local to this call, never touching graph state.
    """
    image_indices = [i for i, m in enumerate(messages) if _is_image_message(m)]
    cutoff = len(image_indices) - max(MAX_IMAGES_IN_CONTEXT, 0)
    if cutoff <= 0:
        return messages

    filtered = list(messages)
    for i in image_indices[:cutoff]:
        filtered[i] = HumanMessage(content=IMAGE_RETENTION_PLACEHOLDER)
    return filtered


def _subtask_state_anchor(turns: list) -> str:
    """Factual state anchor from the LAST browser_* ToolMessage in `turns`
    (URL + visible affordances) — reuses _apply_history_diff's own
    extraction rather than a new parser. Added because _summarize_subtask
    used to be narrative-only (intent + attempted actions + a generic
    verdict) and never captured where a subtask actually LEFT the page;
    the A4 negative result traced a stale summary to exactly this gap
    (docs/engineering-log.md, "A4 / COMPACTION ... RÉSULTAT NÉGATIF NET").
    Empty string if no structural browser_* result is found in range —
    same degrade-gracefully-to-nothing rule as the rest of this module."""
    browser_indices = _browser_result_indices(turns)
    if not browser_indices:
        return ""
    try:
        result = json.loads(turns[browser_indices[-1]].content)
    except (json.JSONDecodeError, TypeError):
        return ""
    text = _browser_result_text(result)
    if not _is_structural_browser_result(text):
        return ""
    url = _extract_page_url(text)
    affordances = _extract_affordances_structured(text)
    sample = ", ".join(f'{a["kind"]} "{a["label"]}"' for a in affordances[:5]) or "(aucun)"
    more = f" (+{len(affordances) - 5} autres)" if len(affordances) > 5 else ""
    return f"état constaté en fin de sous-tâche : URL={url or 'inconnue'} ; éléments visibles : {sample}{more}."


def _summarize_subtask(subtask: dict, turns: list) -> str:
    """Structured summary replacing a completed subtask's raw turns (see
    _apply_episode_compaction): description, key actions distilled from
    the AI messages' tool_calls in that range (name + first argument
    value, truncated), the subtask's recorded result, and a factual
    state anchor (_subtask_state_anchor) — appended only when found."""
    actions = []
    for m in turns:
        for call in getattr(m, "tool_calls", None) or []:
            args = call.get("args") or {}
            hint = str(next(iter(args.values()), ""))[:40]
            actions.append(f"{call.get('name', '?')}({hint})" if hint else call.get("name", "?"))
    result = subtask.get("result") or "(résultat non consigné)"
    summary = (
        f"[Sous-tâche compactée] {subtask.get('description', '')} — "
        f"actions : {', '.join(actions) or '(aucune)'} — résultat : {result}"
    )
    anchor = _subtask_state_anchor(turns)
    return f"{summary} — {anchor}" if anchor else summary


def _active_subtask_index(plan: list) -> Optional[int]:
    """Index of the plan's "en_cours" subtask, or None (none/empty plan) —
    plan invariant: at most one "en_cours" subtask at a time. Used by
    _apply_episode_compaction to find where the active (not-yet-complete)
    subtask's turns begin, so they're never compacted away."""
    return next((i for i, st in enumerate(plan) if st.get("status") == "en_cours"), None)


def _apply_episode_compaction(messages: list, plan: list, subtask_message_start: list) -> list:
    """
    Beyond EPISODE_COMPACTION_TURN_THRESHOLD messages, replaces each
    COMPLETED ("fait"/"echoue") subtask's raw message range with one
    summary message (_summarize_subtask) — same transient-filter
    principle as _apply_image_retention (new list, checkpointer never
    touched). The active subtask's turns and anything not yet attributed
    to a completed subtask are left untouched, so is the objective
    (always before subtask_message_start[0]). No-op if disabled, under
    threshold, or subtask_message_start doesn't cover the plan (index out
    of range — a plan/boundary desync should degrade to "compact
    nothing", never raise mid-task).
    """
    if not EPISODE_COMPACTION_ENABLED or len(messages) <= EPISODE_COMPACTION_TURN_THRESHOLD:
        return messages

    active_index = _active_subtask_index(plan)
    limit = subtask_message_start[active_index] if active_index is not None and active_index < len(
        subtask_message_start
    ) else len(messages)

    ranges = []
    for i, start in enumerate(subtask_message_start):
        if i >= len(plan) or plan[i].get("status") not in ("fait", "echoue"):
            continue
        end = min(subtask_message_start[i + 1] if i + 1 < len(subtask_message_start) else limit, limit)
        if end > start:
            ranges.append((start, end, _summarize_subtask(plan[i], messages[start:end])))
    if not ranges:
        return messages

    compacted = list(messages)
    for start, end, summary in sorted(ranges, key=lambda r: r[0], reverse=True):
        compacted[start:end] = [HumanMessage(content=summary)]
    return compacted


_HISTORY_DIFF_MARKER = "[Observation compactée]"

# Lexical approximation of "an error-like message appeared" — the
# accessibility-tree snapshot carries no color/severity signal, so this
# is honestly a keyword heuristic, not a real error-detection capability.
_ERROR_HINT_RE = re.compile(
    r"\b(erreur|error|invalide|invalid|échec|echec|failed|obligatoire|required|manquant|missing)\b",
    re.IGNORECASE,
)


def _browser_result_text(result: dict) -> str:
    """Concatenated text blocks of a browser_* tool result dict — the same
    surface _extract_page_url/_extract_affordances_structured already
    parse, reused here rather than a new snapshot parser."""
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


def _is_structural_browser_result(text: str) -> bool:
    """True if `text` looks like a real page snapshot (a URL line or at
    least one affordance) rather than synthetic feedback — a guardrail
    rejection (_fabrication_feedback) or an mcp-client error carries no
    page state to diff against."""
    return bool(_extract_page_url(text)) or bool(_extract_affordances_structured(text))


def _diff_browser_observation(prev_result: dict, curr_result: dict) -> str:
    """
    Structural, harness-computed diff between two consecutive STRUCTURAL
    browser_* results (Effort 2, docs/briefs/scaffolding-optimisation.md)
    — no LLM call, reuses the existing snapshot parsers. Grounded in what
    the accessibility-tree snapshot actually exposes: URL change,
    affordances appeared/disappeared (kind+label identity — no existing
    helper exposes element VALUES, so a value-only change on an
    unchanged label is invisible here), and a lexical error-hint heuristic
    (see _ERROR_HINT_RE, not a real severity/color signal).
    """
    prev_text, curr_text = _browser_result_text(prev_result), _browser_result_text(curr_result)
    facts = []

    prev_url, curr_url = _extract_page_url(prev_text), _extract_page_url(curr_text)
    if curr_url and curr_url != prev_url:
        facts.append(f"URL changée ({prev_url or 'inconnue'} → {curr_url})")

    prev_keys = {(i["kind"], i["label"]) for i in _extract_affordances_structured(prev_text)}
    curr_keys = {(i["kind"], i["label"]) for i in _extract_affordances_structured(curr_text)}
    for label, keys in (("apparu(s)", curr_keys - prev_keys), ("disparu(s)", prev_keys - curr_keys)):
        if keys:
            sample = ", ".join(f'{kind} "{name}"' for kind, name in list(keys)[:5])
            more = f" (+{len(keys) - 5} autres)" if len(keys) > 5 else ""
            facts.append(f"{label} : {sample}{more}")

    if _ERROR_HINT_RE.search(curr_text) and not _ERROR_HINT_RE.search(prev_text):
        facts.append("nouveau texte évoquant une erreur")

    if not facts:
        return f"{_HISTORY_DIFF_MARKER} aucun changement structurel détecté depuis l'observation précédente."
    return f"{_HISTORY_DIFF_MARKER} " + " ; ".join(facts) + "."


def _browser_result_indices(messages: list) -> list:
    """Indices of every browser_* ToolMessage in `messages` — a
    ToolMessage carries no tool name, so identity is resolved via the
    preceding AIMessage's tool_calls (same technique as
    _previous_turn_tool_calls). Shared by _apply_history_diff and its
    unconditional coverage judge in call_llm."""
    id_to_name = {
        tc.get("id"): tc.get("name")
        for m in messages
        if getattr(m, "type", None) == "ai"
        for tc in (getattr(m, "tool_calls", None) or [])
    }
    return [
        i
        for i, m in enumerate(messages)
        if getattr(m, "type", None) == "tool"
        and (id_to_name.get(getattr(m, "tool_call_id", None)) or "").startswith("browser_")
    ]


# Only these browser_* tools produce page-SNAPSHOT-shaped text (a "Page
# URL:" line and/or affordance lines) that _diff_browser_observation can
# meaningfully compare. browser_evaluate/browser_run_code_unsafe/
# browser_extract/browser_inspect/browser_take_screenshot return an
# arbitrary JSON/text/image PAYLOAD instead — _is_structural_browser_result
# always reads those as "non-structural" (no URL/affordance line to find),
# so compacting them fell into the SAME bucket as a genuine guardrail
# rejection: "pas de page renvoyée à ce tour (action bloquée ou erreur)".
# Confirmed live (2026-09-18, docs/engineering-log.md, "HISTORY_
# DIFF_ENABLED closing campaign" / T10 root cause): a browser_evaluate
# result holding the actual extracted book list was erased this way,
# reading to the model as "nothing happened" — it then re-fetched the
# same data via repeated failing calls, unable to recall it had already
# succeeded. These tools' results are data the model must be able to
# recall verbatim, not page state to diff — excluded from compaction
# entirely rather than taught a second diff shape no page-comparison
# logic actually fits.
_SNAPSHOT_SHAPED_BROWSER_TOOLS = {"browser_navigate", "browser_click", "browser_snapshot"}


def _apply_history_diff(messages: list) -> list:
    """
    Replaces every PAST snapshot-shaped browser_* tool result (all but
    the most recent, see _SNAPSHOT_SHAPED_BROWSER_TOOLS) in the outbound
    copy with a short structural diff against its nearest STRUCTURAL
    predecessor (_diff_browser_observation) — same transient-filter
    principle as _apply_image_retention/_apply_episode_compaction (new
    list, checkpointer never touched, SAME LENGTH: only
    ToolMessage.content is replaced, never inserted/removed, so
    subtask_message_start indices computed on the raw history stay valid
    regardless of filter order). A non-structural past result (guardrail
    feedback, mcp-client error) is never used as a diff baseline and gets
    a fixed neutral note instead of a fabricated comparison; the first
    structural result gets a fixed "first observation" note rather than a
    diff against nothing (which would just relist everything as
    "appeared"). No-op if disabled or fewer than 2 eligible results exist
    (nothing "past" to compact yet).
    """
    if not HISTORY_DIFF_ENABLED:
        return messages
    browser_indices = _browser_result_indices(messages)
    if len(browser_indices) <= 1:
        return messages
    id_to_name = {
        tc.get("id"): tc.get("name")
        for m in messages
        if getattr(m, "type", None) == "ai"
        for tc in (getattr(m, "tool_calls", None) or [])
    }
    eligible_indices = [
        idx
        for idx in browser_indices
        if id_to_name.get(getattr(messages[idx], "tool_call_id", None)) in _SNAPSHOT_SHAPED_BROWSER_TOOLS
    ]
    if len(eligible_indices) <= 1:
        return messages

    filtered = list(messages)
    last_structural_result = None
    for pos, idx in enumerate(eligible_indices):
        is_latest = pos == len(eligible_indices) - 1
        try:
            result = json.loads(messages[idx].content)
        except (json.JSONDecodeError, TypeError):
            result = {}
        text = _browser_result_text(result)
        structural = _is_structural_browser_result(text)
        if not is_latest:
            if not structural:
                replacement = (
                    f"{_HISTORY_DIFF_MARKER} pas de page renvoyée à ce tour "
                    "(action bloquée ou erreur) — aucun changement de page à signaler."
                )
            elif last_structural_result is None:
                replacement = f"{_HISTORY_DIFF_MARKER} première observation de la page (remplacée par les tours suivants)."
            else:
                replacement = _diff_browser_observation(last_structural_result, result)
            filtered[idx] = messages[idx].model_copy(update={"content": replacement})
        if structural:
            last_structural_result = result
    return filtered


def _previous_turn_tool_calls(messages: list) -> Optional[list]:
    """Last AI message with tool_calls in the history — the turn that led to this call_llm invocation."""
    for message in reversed(messages):
        if getattr(message, "type", None) == "ai" and getattr(message, "tool_calls", None):
            return message.tool_calls
    return None


def _should_suppress_thinking(messages: list, session_grants) -> bool:
    """
    True when ADAPTIVE_THINKING is enabled AND the previous turn was
    fully auto-approved (same tier policy as has_tool_calls) — typically
    a repeated read/reversible tool loop where extended reasoning costs
    more than it's worth. Applied by call_llm via
    bound_llm.bind(extra_body={"enable_thinking": False}), a real
    per-request parameter (kwargs passed to Runnable.bind() override
    _default_params in langchain_openai's _get_request_payload, verified
    against the installed langchain-openai==0.2.2) — never a prompt-level
    injection, so no message-ordering constraint to satisfy, unlike the
    text-prefix mechanism this replaced. False on a task's very first
    turn (no previous tool_calls) or as soon as a sensitive tool was
    involved: reasoning has the most value there.
    """
    if not ADAPTIVE_THINKING:
        return False
    previous_tool_calls = _previous_turn_tool_calls(messages)
    if not previous_tool_calls:
        return False
    return all(
        approval_policy.is_auto_approved(tc["name"], tc.get("args"), session_grants)
        for tc in previous_tool_calls
    )


_PLAN_STATUS_MARKERS = {"fait": "[x]", "en_cours": "[>]", "a_faire": "[ ]"}


def _render_plan(plan: list) -> list[dict]:
    """
    Plain index/description/success_criterion/status view of `plan` —
    shared by _merged_plan_directive (rendered into the system prompt)
    and the manage_plan tool response (reverberated to the model after
    set_plan/complete_subtask), so both stay in sync by construction
    instead of two independent renderings drifting apart.
    """
    return [
        {
            "index": i,
            "description": st["description"],
            "success_criterion": st["success_criterion"],
            "status": st.get("status", "a_faire"),
        }
        for i, st in enumerate(plan)
    ]


def _merged_plan_directive(state: AgentState) -> str:
    """
    Persistent PLAN section for merged-planning mode: the full subtask
    list with status, not just the active one — an editable document for
    manage_plan to operate on (the AgentOccam pattern this mode follows,
    see docs/briefs/update-plan.md "2.1 addendum"), regenerated from
    state every turn. Rendered even with an empty plan (a "nothing yet"
    template) so the very first manage_plan call has a document to
    compose into rather than acting on an instruction alone. No-op
    outside merged mode (empty string, byte-for-byte unchanged
    elsewhere).

    Deliberately states the tool's purpose, not a command to use it now
    or first: an explicit "your first action MUST be manage_plan, NEVER
    call anything else before" wording was tried (docs/history.md,
    EFFORT 2 point 3) and measured ineffective (merged_plan_calls stayed
    0 even under that constraint) — and forcing it crosses the "don't
    make manage_plan mandatory" rule regardless of outcome, since it
    would measure obedience, not adoption.
    """
    if PLANNING_MODE != "merged":
        return ""
    plan = state.get("plan") or []
    if not plan:
        return (
            "\n### PLAN (mode planification fusionnée)\n"
            "Aucune sous-tâche pour l'instant — document modifiable via "
            "l'outil manage_plan (set_plan pour le composer, "
            "complete_subtask pour faire avancer la sous-tâche active une "
            "fois le plan posé).\n"
        )
    lines = ["\n### PLAN (mode planification fusionnée)"]
    for st in _render_plan(plan):
        marker = _PLAN_STATUS_MARKERS.get(st["status"], "[ ]")
        lines.append(f'{marker} {st["index"]}. {st["description"]} — critère : "{st["success_criterion"]}"')
    return "\n".join(lines) + "\n"


async def call_llm(state: AgentState, config: dict) -> dict:
    bound_llm = await _get_bound_llm()
    # Compacted BEFORE the system message is prepended: subtask_message_start
    # indices are relative to state["messages"] (see _apply_episode_compaction).
    raw_message_count = len(state["messages"])
    compacted_messages = _apply_episode_compaction(
        state["messages"], state.get("plan") or [], state.get("subtask_message_start") or []
    )
    # Coverage judge for episode compaction (PLAN.md Phase 2, point 2):
    # logged on EVERY call_llm invocation, regardless of
    # EPISODE_COMPACTION_ENABLED — a campaign run with the flag OFF still
    # needs this to answer "would compaction even have triggered here?"
    # before its result can be read as a real measurement of the
    # mechanism (see docs/campaigns/2026-07-28_campaign_episode-
    # compaction-enabled.md, requalified "non concluant" after only
    # 9-15% of runs were estimated to cross the threshold).
    audit_log.log_message(
        config.get("configurable", {}).get("thread_id", ""),
        "episode_compaction",
        {"messages_count": raw_message_count, "compacted": len(compacted_messages) < raw_message_count},
    )
    messages_for_llm = [
        SystemMessage(
            content=(
                # _merged_plan_directive LAST (empty string outside
                # PLANNING_MODE="merged" — no effect on any other mode's
                # prompt, byte-for-byte): it now renders the full plan
                # state (changes every turn a subtask completes), so it
                # sits after the static directives and the date to keep
                # that prefix cacheable. An earlier version put it FIRST
                # for primacy (see docs/history.md, EFFORT 2 point 3):
                # superseded by the persistent-section redesign, not
                # stacked with it.
                f"{DOWNLOAD_DIRECTIVE}{BULK_CHECK_DIRECTIVE}{PEREMPTION_DIRECTIVE}"
                f"{_date_directive()}{_merged_plan_directive(state)}"
            )
        )
    ] + compacted_messages
    messages_for_llm = _apply_image_retention(messages_for_llm)
    history_diffed = _apply_history_diff(messages_for_llm)
    # Coverage judge for history diff (Effort 2, docs/briefs/
    # scaffolding-optimisation.md): logged on EVERY call_llm invocation,
    # regardless of HISTORY_DIFF_ENABLED — same discipline as episode
    # compaction above, per CLAUDE.md's trigger-rate-counter rule.
    # browser_messages_count is computed on RAW state["messages"] (the
    # true opportunity size, independent of episode compaction);
    # messages_replaced is computed on messages_for_llm (the actual
    # effect of this call, downstream of episode compaction if both are
    # ever enabled together).
    # total_messages_count alongside browser_messages_count lets a reader
    # compute a REDUNDANCY DENSITY (browser_messages_count /
    # total_messages_count) instead of just an absolute opportunity size —
    # distinguishes "few browser_* results because the task is short" from
    # "few browser_* results despite a long conversation dominated by
    # something else", the exact ambiguity that made A1/A2's "mixed, not
    # decisive" reading (docs/engineering-log.md, "HISTORY-DIFF LIVE
    # SMOKE") hard to separate from a broken mechanism without a live
    # re-run.
    audit_log.log_message(
        config.get("configurable", {}).get("thread_id", ""),
        "history_diff",
        {
            "browser_messages_count": len(_browser_result_indices(state["messages"])),
            "total_messages_count": len(state["messages"]),
            "messages_replaced": sum(1 for a, b in zip(messages_for_llm, history_diffed) if a is not b),
        },
    )
    messages_for_llm = history_diffed
    # Coverage judge for adaptive thinking (retroactive per CLAUDE.md's
    # trigger-rate-counter rule — this mechanism predates it): logged on
    # EVERY call_llm invocation regardless of ADAPTIVE_THINKING, same
    # discipline as episode_compaction/history_diff above.
    suppress_thinking = _should_suppress_thinking(messages_for_llm, state.get("session_grants") or [])
    audit_log.log_message(
        config.get("configurable", {}).get("thread_id", ""),
        "adaptive_thinking",
        {"suppressed": suppress_thinking},
    )
    # Single extra_body dict for both mechanisms rather than two separate
    # .bind() calls: chaining .bind(extra_body={...}) twice would let the
    # second call's extra_body silently replace the first's (LangChain
    # merges top-level bind() kwargs, not their nested dict values) —
    # would have silently dropped enable_thinking:False whenever both
    # ADAPTIVE_THINKING and REASONING_EFFORT fire on the same turn.
    extra_body = {}
    if suppress_thinking:
        extra_body["enable_thinking"] = False
    if REASONING_EFFORT:
        extra_body["reasoning_effort"] = REASONING_EFFORT
    if extra_body:
        bound_llm = bound_llm.bind(extra_body=extra_body)
    # Carried over as-is from the previous call within this turn (see
    # AgentState.think_opened/think_closed) rather than reset to False, so
    # as to produce only one continuous <think> tag even if call_llm loops
    # several times via AUTO_APPROVED_TOOLS.
    token = _think_state.set(
        {"opened": state.get("think_opened", False), "closed": state.get("think_closed", False)}
    )
    try:
        merged = None
        async for chunk in bound_llm.astream(messages_for_llm):
            merged = chunk if merged is None else merged + chunk
    finally:
        think = _think_state.get()
        _think_state.reset(token)

    # Only forces the closing tag here if this turn won't re-trigger
    # call_llm (no tool_calls): otherwise we'd prematurely cut a <think>
    # meant to continue on the next iteration of the auto-approved tool
    # loop. The "tool_calls + human approval pause" case is handled
    # separately on the streamed-response side (see needs_closing_tag,
    # app/main.py).
    if think["opened"] and not think["closed"] and not getattr(merged, "tool_calls", None):
        merged.content += "</think>"
        think["closed"] = True

    # Safety net (see MAX_EMPTY_ANSWER_RETRIES above for the root cause):
    # the model sometimes wrote its tool call in prose instead of letting
    # the server's grammar recognize it. Before counting this turn as a
    # failure (see has_tool_calls), an attempt is made to recover the
    # intent rather than lose the turn.
    if not getattr(merged, "tool_calls", None):
        fallback = _extract_fallback_tool_call(merged.content)
        if fallback:
            logger.warning(
                "Fallback tool call extracted from an unstructured response "
                "(tool=%s, args=%s): the model wrote its call in prose "
                "instead of emitting an OpenAI tool_calls the server recognizes.",
                fallback["name"],
                fallback["args"],
            )
            merged.tool_calls = [fallback]

    # Observability (revised Phase 1d, see docs/history.md "extraction fix"
    # -> "OBSERVABILITY"): persists THIS model turn (<think> reasoning +
    # text + any tool_calls), whether it's then auto-approved, submitted
    # for approval, or rejected — unlike the tool_calls log
    # (log_tool_call, every tier since docs/resolved-bugs.md #52), this
    # trace covers PROPOSED calls too: it's the agent's reasoning, never a
    # side effect to filter.
    thread_id = config.get("configurable", {}).get("thread_id", "")
    audit_log.log_message(
        thread_id,
        "assistant",
        {"content": merged.content, "tool_calls": getattr(merged, "tool_calls", None)},
    )

    return {
        "messages": [merged],
        "think_opened": think["opened"],
        "think_closed": think["closed"],
        # Reset to False on every call: it's the only other node that ends
        # a turn on a visible AIMessage (see
        # AgentState.slash_command_image_shown) — without this reset, a
        # normal LLM turn that follows an image (e.g. vision on a
        # model-decided browser_take_screenshot) would wrongly reuse main.py's image
        # reconstruction, duplicating the image in its own already-correct
        # response.
        "slash_command_image_shown": False,
    }


def has_tool_calls(state: AgentState) -> str:
    last = state["messages"][-1]
    tool_calls = getattr(last, "tool_calls", None)
    if not tool_calls:
        # "Empty answer" safety net (see MAX_EMPTY_ANSWER_RETRIES): no
        # tool_calls (even after call_llm's fallback extraction attempt)
        # AND nothing visible outside <think> -> loop back to call_llm
        # rather than giving up immediately, as long as the retry budget
        # isn't exhausted.
        if not has_visible_answer(last.content) and state.get("empty_answer_retries", 0) < MAX_EMPTY_ANSWER_RETRIES:
            return "retry_empty_answer"
        return "end"
    if state["tool_iterations"] >= MAX_TOOL_ITERATIONS:
        return "end"
    grants = state.get("session_grants") or []
    all_auto_approved = all(
        approval_policy.is_auto_approved(tc["name"], tc.get("args"), grants) for tc in tool_calls
    )
    # The "virtual keyboard" guardrail (see AUTO_APPROVAL_STREAK_LIMIT):
    # even a fully auto-approved turn goes back through require_approval
    # once the cap on consecutive unsupervised turns is reached.
    if all_auto_approved and state.get("auto_approval_streak", 0) < AUTO_APPROVAL_STREAK_LIMIT:
        return "auto_call_tools"
    return "call_tools"


async def retry_empty_answer(state: AgentState) -> dict:
    """
    Point de reboucle du filet de sécurité "réponse vide" (voir
    MAX_EMPTY_ANSWER_RETRIES). Remet aussi think_opened/think_closed à False
    pour que la nouvelle tentative reparte sur une balise <think> fraîche —
    sans ça, le raisonnement du retry s'afficherait en texte brut (déjà
    "opened" selon l'état persisté par la tentative ratée), invisible en
    dehors d'une bulle repliable.
    """
    return {
        "empty_answer_retries": state.get("empty_answer_retries", 0) + 1,
        "think_opened": False,
        "think_closed": False,
    }


async def require_approval(state: AgentState) -> dict:
    """Pause point: blocks until a human has approved/rejected (see app/main.py)."""
    if state.get("approved") is None:
        raise NodeInterrupt("Approbation humaine requise avant exécution d'outil.")
    # A human actually went through: resets the consecutive auto-approved
    # turns budget (see AUTO_APPROVAL_STREAK_LIMIT).
    updates = {"messages": [], "auto_approval_streak": 0, "grant_session": False}
    # "approve for the session" (Phase 3): the pending turn's tools join
    # session_grants, capped at TIER_REVERSIBLE (auto + audit) for the
    # rest of the thread — see approval_policy.effective_tier() and
    # AgentState.session_grants. The turn itself stays subject to THIS
    # approval (a grant only applies starting from the NEXT call of the
    # same tool, not retroactively to the one that requested it).
    if state.get("grant_session"):
        last = state["messages"][-1]
        granted_names = {tc["name"] for tc in last.tool_calls}
        updates["session_grants"] = list(set(state.get("session_grants") or []) | granted_names)
    return updates


def route_after_approval(state: AgentState) -> str:
    return "call_tools" if state["approved"] else "reject_tools"


def _to_png_data_uri(data_b64: str, mime_type: str) -> str:
    """
    Always re-encodes to PNG before passing to the LLM. Ollama's image
    decoder (mtmd, llama.cpp side) explicitly fails on WebP ("Failed to
    load image or audio file") — which happens to be
    browser_take_screenshot's default format. Converting here rather than
    relying on the model to systematically request format="png" on every
    call. Default path (IMAGE_FORMAT_PASSTHROUGH not enabled) — see
    _to_image_data_uri for the direct WebP path.
    """
    if mime_type == "image/png":
        return f"data:image/png;base64,{data_b64}"
    raw = base64.b64decode(data_b64)
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}"


def _to_image_data_uri(data_b64: str, mime_type: str) -> str:
    """
    IMAGE_FORMAT_PASSTHROUGH=webp: passes browser_take_screenshot's raw WebP through
    as-is (direct data URI, no Pillow decode/re-encode), relying on the
    native WebP decoding of the llama.cpp fork served by the alternative
    llama-server backend (see README, Inference backend section) — avoids
    the CPU cost of PNG reconversion on every capture. Default (variable
    absent/different from "webp", the case for both TabbyAPI and Ollama):
    systematic PNG conversion via _to_png_data_uri.
    """
    if IMAGE_FORMAT_PASSTHROUGH:
        return f"data:{mime_type};base64,{data_b64}"
    return _to_png_data_uri(data_b64, mime_type)


def _split_image_blocks(result: dict) -> tuple[dict, list[dict]]:
    """
    Splits image blocks (MCP format: {"type": "image", "data": <base64>,
    "mimeType": ...}) out of the rest of the tool result. A ToolMessage
    (role "tool") can only hold OpenAI-compatible text — putting the raw
    base64 in there (via json.dumps on the whole result, as before)
    produces an unreadable text blob for the model, whether it's
    multimodal or not. Images are reinjected separately as a multimodal
    "user" message (see call_tools), the only role that supports an
    image_url block.
    """
    content = result.get("content")
    if not isinstance(content, list):
        return result, []
    images = [b for b in content if isinstance(b, dict) and b.get("type") == "image"]
    if not images:
        return result, []
    rest = [b for b in content if b not in images]
    return {**result, "content": rest or "(voir image ci-dessous)"}, images


def _format_ocr_detections(detections: list) -> str:
    """Model-facing text (French, like every other tool-facing string in
    this module — CLAUDE.md rule 11): one line per detection, its
    bounding box then its text, so the model can target
    browser_mouse_click_xy/_move_xy at the right pixel without ever
    having seen the image itself."""
    if not detections:
        return "(aucun texte détecté par OCR sur cette capture)"
    lines = [
        f'[{d["x"]},{d["y"]},{d["width"]},{d["height"]}] "{d["text"]}" (confiance {d["confidence"]:.2f})'
        for d in detections
    ]
    return 'Texte détecté (OCR) — [x,y,largeur,hauteur] "texte" (confiance) :\n' + "\n".join(lines)


async def _ocr_replace_image_blocks(
    client: httpx.AsyncClient, content: list, thread_id: Optional[str], tool_name: str
) -> list:
    """VISUAL_NAVIGATION_ONLY (docs/briefs/visual-navigation-only.md):
    replaces every image block (a real screenshot — an explicit
    browser_take_screenshot call, or mcp-client's own stabilization
    follow-up after browser_navigate, see services/mcp-client/app/
    main.py) with its OCR reading, text + bounding box, nothing else the
    model can see. Never raises: ocr-service unreachable degrades to a
    plain-text notice in place of the image, same fail-open posture as
    _get_tools_schema above — a stalled OCR call must not stall the whole
    conversation.
    """
    out = []
    ocr_calls = 0
    for block in content:
        if not (isinstance(block, dict) and block.get("type") == "image"):
            out.append(block)
            continue
        try:
            resp = await client.post(
                f"{OCR_SERVICE_URL}/ocr",
                json={"image_base64": block.get("data", ""), "mime_type": block.get("mimeType", "image/png")},
            )
            resp.raise_for_status()
            detections = resp.json()
        except (httpx.HTTPError, ValueError):
            out.append({"type": "text", "text": "(OCR indisponible pour cette capture)"})
            continue
        ocr_calls += 1
        out.append({"type": "text", "text": _format_ocr_detections(detections)})
    if ocr_calls:
        # Trigger-rate counter (CLAUDE.md measurement rules). Unlike
        # history_diff/episode_compaction's "log every call regardless of
        # the flag" (there, a real off-state opportunity exists to
        # compare against), this code path only exists at all when
        # VISUAL_NAVIGATION_ONLY is already true — logging is
        # unconditional within that scope, which is the whole point:
        # confirms the mode was genuinely active for the run it's judged
        # on, not a flattering zero.
        audit_log.log_message(thread_id or "", "visual_navigation_only", {"tool": tool_name, "ocr_calls": ocr_calls})
    return out


async def _call_mcp_tool(
    client: httpx.AsyncClient,
    tool_name: str,
    args: dict,
    thread_id: Optional[str] = None,
    worker_id: Optional[str] = None,
) -> tuple[dict, list]:
    """
    Single HTTP call to mcp-client:/call, factored out between
    _execute_tool_calls (tool_calls decided by the LLM) and
    run_slash_command_direct (command typed directly by the user) — same
    error handling/image-block splitting in both cases.

    thread_id (optional): forwarded so mcp-client can key its visual-
    feedback capture by it (docs/briefs/campaign-visual-feedback.md) —
    unrelated to this function's own return value, never touches
    image-block splitting below. Omitted by callers with no thread_id in
    scope (e.g. _fetch_verification_snapshot), which simply get no
    capture for that call.

    worker_id (optional, effort 1.3, docs/briefs/
    effort-1.3-parallel-campaigns.md): forwarded so mcp-client can scope
    its persistent "browser" session per parallel-campaign worker instead
    of one shared session for every caller. Omitted (the overwhelming
    common case — interactive Open WebUI, a non-parallel campaign) falls
    back to mcp-client's own "default" bucket, identical to pre-effort-1.3
    behavior.
    """
    try:
        resp = await client.post(
            f"{MCP_CLIENT_URL}/call",
            json={"tool": tool_name, "arguments": args, "thread_id": thread_id, "worker_id": worker_id},
        )
        resp.raise_for_status()
        result = resp.json()
    except httpx.HTTPError as exc:
        return {"error": str(exc)}, []
    if VISUAL_NAVIGATION_ONLY:
        content = result.get("content")
        if isinstance(content, list):
            result = {**result, "content": await _ocr_replace_image_blocks(client, content, thread_id, tool_name)}
        return result, []
    return _split_image_blocks(result)


async def _execute_tool_calls(state: AgentState, config: dict) -> dict:
    """
    Logic shared between call_tools (reached after require_approval) and
    auto_call_tools (reached directly from has_tool_calls, never seen by
    a human THIS turn). Logs (app/audit_log.py) every real tool_call
    dispatched to mcp-client, whatever its effective tier — including
    those coming from call_tools. TIER_READ calls used to be excluded
    ("silent by design, nothing new to trace"), which left every
    wrapper-dispatched read tool (browser_extract, browser_inspect,
    browser_snapshot, browser_take_screenshot, the filesystem reads)
    invisible to any archive analysis keyed on the `"tool"` field, e.g.
    scripts/analyze-tool-call-ngrams.sh — see docs/resolved-bugs.md #52.

    Blind spot fixed (see docs/history.md, T9 investigation): this node
    used to audit-log ONLY auto_call_tools's tool_calls, on the grounds
    that a turn that went through require_approval already has its trace
    in the conversation history ("⚠️ Approbation requise" + the answer).
    That reasoning assumes an actual human saw the request go by — in an
    automated campaign, `_approve(..., grant_session=True)` (the harness)
    plays that role with no human ever looking, and the conversation
    history itself doesn't survive a service restart (MemorySaver
    checkpointer, in-memory only): the audit log then remains the ONLY
    persistent trace, including for the very first call of each tool per
    thread — invisible until now in both cases.
    """
    last = state["messages"][-1]
    new_messages = []
    grants = state.get("session_grants") or []
    thread_id = config.get("configurable", {}).get("thread_id", "")
    worker_id = config.get("configurable", {}).get("worker_id")

    # URL-fabrication guardrail (Phase 1): scope = URLs already observed
    # THIS turn/previous turns of the task + scope roots (1st human
    # message). Recomputed/extended as THIS turn's tool_calls are
    # processed (several browser_* calls can appear in the same
    # tool_calls list).
    #
    # "First hop" fix (see docs/history.md, browser-session reliability
    # effort): `has_prior_navigation` distinguishes the persisted raw set
    # (navigations ACTUALLY already performed) from the union with
    # `_task_scope_urls` below — used to exempt the task's very FIRST
    # navigation from the guardrail (see below), not just those to a URL
    # already mentioned in the prompt. Root cause: real tasks with no URL
    # in the prompt (T8 "on Wikipedia...", T11 "what's the latest Python
    # version?") had THEIR VERY FIRST navigation, though legitimate,
    # blocked as fabrication — mistaken during diagnosis for a
    # playwright-mcp infra failure before tracing it back to the actual
    # tool result (the guardrail's own rejection message).
    has_prior_navigation = bool(state.get("observed_urls"))
    observed_urls = set(state.get("observed_urls") or []) | _task_scope_urls(state["messages"])
    current_page_url = state.get("current_page_url")
    current_page_links = state.get("current_page_links") or []
    fabricated_attempts = 0
    # Task objective (see _prioritize_affordances): the 1st human message,
    # for lack of explicit subtasks (full Phase 1 not done yet — this
    # finer breakdown will come with the planner node).
    first_human = next((m for m in state["messages"] if getattr(m, "type", None) == "human"), None)
    objective = first_human.content if first_human and isinstance(first_human.content, str) else ""

    plan = state.get("plan") or []
    # Merged-planning mode only (PLANNING_MODE="merged", see manage_plan
    # dispatch below): tracks whether this turn's tool_calls actually
    # mutated the plan, so the returned dict only includes "plan"/
    # "subtask_message_start" when there's something new to report —
    # every other mode's return shape stays byte-for-byte unchanged.
    plan_changed = False
    subtask_message_start = state.get("subtask_message_start") or []

    async with httpx.AsyncClient(timeout=60) as client:
        for tool_call in last.tool_calls:
            if tool_call["name"] == _MANAGE_PLAN_TOOL_NAME:
                # Merged-planning mode's entire planning/replanning/
                # completion responsibility (PLANNING_MODE="merged",
                # docs/briefs/update-plan.md "2.1 addendum") — never
                # dispatched to mcp-client, mutates `plan` synchronously.
                # TIER_READ (approval_policy.tool_tier), so this never
                # reaches require_approval.
                args = tool_call.get("args") or {}
                action = args.get("action")
                response: dict
                if action == "set_plan":
                    candidate = [
                        {
                            "description": st.get("description", ""),
                            "success_criterion": st.get("success_criterion", ""),
                        }
                        for st in (args.get("subtasks") or [])
                        if isinstance(st, dict)
                    ]
                    schema = await _get_tools_schema()
                    known_tools = {t.get("function", {}).get("name") for t in schema}
                    known_tools.discard(None)
                    reasons = plan_validation.validate_plan_heuristics(
                        candidate, known_tools=known_tools, task_scope_urls=_task_scope_urls(state["messages"])
                    )
                    audit_log.log_message(
                        thread_id,
                        "merged_planning",
                        {
                            "action": "set_plan",
                            "subtask_count": len(candidate),
                            "heuristic_rejected": bool(reasons),
                            "subtask_index": None,
                        },
                    )
                    if reasons:
                        response = {"error": "plan rejeté", "reasons": reasons}
                    else:
                        plan = [{**st, "status": "a_faire", "attempts": 0, "result": None} for st in candidate]
                        plan[0]["status"] = "en_cours"
                        subtask_message_start = [len(state["messages"])]
                        plan_changed = True
                        # Full plan reverberated, not a bare {"ok": true}:
                        # the model must see the outcome of its own edit
                        # to make the tool usable next turn (same shape as
                        # _render_plan's system-prompt rendering above).
                        response = {"ok": True, "plan": _render_plan(plan)}
                elif action == "complete_subtask":
                    idx = args.get("subtask_index")
                    if not isinstance(idx, int) or not (0 <= idx < len(plan)) or plan[idx].get("status") != "en_cours":
                        response = {"error": f"sous-tâche {idx!r} invalide ou non active"}
                    else:
                        plan = [dict(st) for st in plan]
                        plan[idx]["status"] = "fait"
                        if idx + 1 < len(plan):
                            plan[idx + 1]["status"] = "en_cours"
                        plan_changed = True
                        response = {"ok": True, "plan": _render_plan(plan)}
                    audit_log.log_message(
                        thread_id,
                        "merged_planning",
                        {
                            "action": "complete_subtask",
                            "subtask_count": len(plan),
                            "heuristic_rejected": False,
                            "subtask_index": idx,
                        },
                    )
                else:
                    response = {"error": f"action inconnue: {action!r}"}
                new_messages.append(
                    {"role": "tool", "tool_call_id": tool_call["id"], "content": json.dumps(response, ensure_ascii=False)}
                )
                continue

            tier = approval_policy.effective_tier(tool_call["name"], tool_call.get("args"), grants)

            blocked = False
            if (
                BROWSER_NAVIGATE_GUARDRAIL
                and has_prior_navigation
                and tool_call["name"] == "browser_navigate"
                and tool_call.get("args", {}).get("url")
                and tool_call["args"]["url"] not in observed_urls
            ):
                blocked = True
                fabricated_attempts += 1
                attempt_number = state.get("fabricated_navigation_attempts", 0) + fabricated_attempts
                page_links_for_feedback = current_page_links or sorted(observed_urls)
                feedback = _fabrication_feedback(
                    tool_call["args"]["url"], attempt_number, page_links_for_feedback
                )
                result = {"content": [{"type": "text", "text": feedback}]}
                images = []
            else:
                result, images = await _call_mcp_tool(
                    client, tool_call["name"], tool_call["args"], thread_id, worker_id
                )
                if tool_call["name"].startswith("browser_"):
                    result = _truncate_browser_result(result, BROWSER_TOOL_OUTPUT_MAX_CHARS, objective)
                    for block in result.get("content", []) if isinstance(result.get("content"), list) else []:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block["text"]
                            page_url = _extract_page_url(text)
                            if page_url:
                                current_page_url = page_url
                            page_links = _extract_urls(text, current_page_url)
                            if page_links:
                                current_page_links = sorted(page_links)
                            observed_urls |= page_links
                    if tool_call["name"] == "browser_navigate" and not blocked:
                        observed_urls.add(tool_call["args"]["url"])
                        current_page_url = tool_call["args"]["url"]

            # Logged AFTER execution (see above) to carry the result as
            # seen by the model (already truncated/prioritized above if
            # browser_*) — see app/audit_log.py, "revised Phase 1d". Every
            # tier, TIER_READ included (docs/resolved-bugs.md #52).
            audit_log.log_tool_call(thread_id, tool_call["name"], tool_call["args"], tier, result)

            new_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )
            for image in images:
                new_messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": _to_image_data_uri(image["data"], image.get("mimeType", "image/png"))
                                },
                            }
                        ],
                    }
                )

    result_dict = {
        "messages": new_messages,
        "tool_iterations": state["tool_iterations"] + 1,
        "approved": None,  # rearms the pause for the next tool turn
        # Incremented unconditionally (auto-approved turn or one just
        # approved by a human): require_approval already reset it to 0 in
        # that second case, so this execution correctly restarts at 1
        # (see AUTO_APPROVAL_STREAK_LIMIT).
        "auto_approval_streak": state.get("auto_approval_streak", 0) + 1,
        "observed_urls": sorted(observed_urls),
        "current_page_url": current_page_url,
        "current_page_links": current_page_links,
        "fabricated_navigation_attempts": state.get("fabricated_navigation_attempts", 0) + fabricated_attempts,
    }
    if plan_changed:
        # Merged-planning mode only (see manage_plan dispatch above) —
        # every other mode never sets plan_changed, so this key is absent
        # from the returned dict and state["plan"] stays whatever it
        # already was (nothing else in "nodes" mode ever sets it).
        result_dict["plan"] = plan
        result_dict["subtask_message_start"] = subtask_message_start
    return result_dict


async def call_tools(state: AgentState, config: dict) -> dict:
    """Reached after require_approval (a human or the campaign harness just approved) — see _execute_tool_calls."""
    return await _execute_tool_calls(state, config)


async def auto_call_tools(state: AgentState, config: dict) -> dict:
    """Reached directly from has_tool_calls (no approval this turn) — see _execute_tool_calls."""
    return await _execute_tool_calls(state, config)


async def reject_tools(state: AgentState) -> dict:
    """Mirrors call_tools when the human rejected: synthesizes a rejection, never calls mcp-client."""
    last = state["messages"][-1]
    new_messages = [
        {
            "role": "tool",
            "tool_call_id": tool_call["id"],
            "content": json.dumps({"error": "Rejeté par l'utilisateur"}, ensure_ascii=False),
        }
        for tool_call in last.tool_calls
    ]
    return {
        "messages": new_messages,
        "tool_iterations": state["tool_iterations"] + 1,
        "approved": None,
    }


def _coerce_slash_arg_value(raw: str):
    """int > float > bool ("true"/"false") > string, in that order."""
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    return raw


def _parse_slash_command(content: str) -> Optional[tuple]:
    """
    "/toolname a=1 b=texte" -> ("toolname", {"a": 1, "b": "texte"}).
    None if the content doesn't start with "/" or is empty after the "/".
    shlex.split handles quoted values containing spaces. A token with no
    "=" (malformed argument) is simply ignored (warning logged) rather
    than failing the whole parse of an otherwise valid command.
    """
    if not content or not content.startswith("/"):
        return None
    try:
        tokens = shlex.split(content[1:])
    except ValueError:
        return None
    if not tokens:
        return None
    tool_name = tokens[0]
    args = {}
    for tok in tokens[1:]:
        if "=" not in tok:
            logger.warning("Slash command argument ignored (no '='): %r", tok)
            continue
        key, _, raw_value = tok.partition("=")
        args[key] = _coerce_slash_arg_value(raw_value)
    return tool_name, args


def _format_tool_result_as_text(result: dict) -> str:
    """Extracts the text from {"type": "text", ...} blocks of the tool
    result; failing that (empty result, error, unexpected shape), raw
    indented JSON."""
    blocks = result.get("content", []) if isinstance(result, dict) else []
    if isinstance(blocks, str):
        # _split_image_blocks falls back to this text placeholder when
        # ALL of the result's blocks were images (e.g.
        # browser_take_screenshot alone) — this is already not a list of
        # blocks, return it as-is rather
        # than iterating over its characters (none of which is a "text"
        # dict, so it would silently fall back to a JSON dump of the
        # whole dict).
        return blocks
    texts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
    if texts:
        return "\n".join(texts)
    return json.dumps(result, ensure_ascii=False, indent=2)


async def prepare_slash_command(state: AgentState, config: dict) -> dict:
    """
    Parses the slash command and synthesizes the corresponding tool_calls,
    without executing it yet — tier-based routing
    (_route_slash_command_tier) then decides whether it goes direct
    (run_slash_command_direct) or through the real approval pause
    (require_approval), depending on the tool's tier.
    """
    tool_name, args = _parse_slash_command(state["messages"][-1].content)
    call_id = f"slash_{uuid.uuid4().hex[:12]}"
    return {
        "messages": [
            {"role": "assistant", "content": "", "tool_calls": [{"name": tool_name, "args": args, "id": call_id}]}
        ]
    }


def _route_slash_command_tier(state: AgentState) -> str:
    """
    GUARDRAIL: a slash command on a TIER_SENSITIVE tool (e.g.
    browser_evaluate) does NOT execute directly — it goes through
    require_approval, exactly like a tool_calls decided by the LLM.
    Explicitly typing the command only counts as approval for
    TIER_READ/TIER_REVERSIBLE: the sensitive tier exists precisely to
    impose a separate confirmation before a potentially dangerous action
    (arbitrary JS execution in the page...) — a total bypass would have
    voided this guarantee for any tool, including ones never meant to be
    auto-approved.
    """
    last = state["messages"][-1]
    tool_call = last.tool_calls[0]
    grants = state.get("session_grants") or []
    tier = approval_policy.effective_tier(tool_call["name"], tool_call.get("args"), grants)
    return "sensitive" if tier == approval_policy.TIER_SENSITIVE else "direct"


async def run_slash_command_direct(state: AgentState, config: dict) -> dict:
    """
    Directly executes the tool_calls synthesized by prepare_slash_command
    (read/reversible tier only, see _route_slash_command_tier) — no LLM,
    no approval pause. Ends on a standard-shaped AIMessage (not just the
    raw ToolMessage) to stay compatible with no changes needed to
    main.py, which assumes the last message of a finished turn is an
    AIMessage with visible content (see _stream_response/_current_answer,
    which would otherwise fall back to the "réponse non exploitable"
    notice).
    """
    last = state["messages"][-1]
    tool_call = last.tool_calls[0]
    tool_name, args, call_id = tool_call["name"], tool_call["args"], tool_call["id"]

    # Traceability (parity with auto_call_tools, docs/resolved-bugs.md
    # #52): never influences execution — the sensitive tier has already
    # been ruled out by _route_slash_command_tier before reaching here,
    # so only TIER_READ/TIER_REVERSIBLE calls land here, both logged.
    grants = state.get("session_grants") or []
    tier = approval_policy.effective_tier(tool_name, args, grants)
    thread_id = config.get("configurable", {}).get("thread_id", "")
    worker_id = config.get("configurable", {}).get("worker_id")

    async with httpx.AsyncClient(timeout=60) as client:
        result, images = await _call_mcp_tool(client, tool_name, args, thread_id, worker_id)

    audit_log.log_tool_call(thread_id, tool_name, args, tier, result)

    new_messages = [
        {"role": "tool", "tool_call_id": call_id, "content": json.dumps(result, ensure_ascii=False)},
    ]
    for image in images:
        new_messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": _to_image_data_uri(image["data"], image.get("mimeType", "image/png"))
                        },
                    }
                ],
            }
        )
    # The "user" message above (standard image_url block) is what a
    # future LLM turn on this thread sees — an efficient format for a
    # multimodal model (fixed per-image API cost), NO base64 embedded as
    # raw text in the final assistant message: an earlier attempt
    # embedded the image as markdown directly here, which did make it
    # appear in THIS response, but also persisted it in the history as
    # text — tokenized as ordinary text (tens of thousands of tokens for
    # a single capture) instead of a real image_url block's fixed cost,
    # blowing up the context (32768 tokens exceeded) as early as the next
    # LLM turn on this thread, even with a single image
    # (MAX_IMAGES_IN_CONTEXT=1 never trims THE last image, so no
    # protection is possible in this form). The image display FOR THIS
    # TURN is reconstructed on main.py's side (_render_visible_answer)
    # from this separate "user" message, never by persisting it here a
    # second time.
    new_messages.append({"role": "assistant", "content": _format_tool_result_as_text(result)})

    return {
        "messages": new_messages,
        "tool_iterations": state["tool_iterations"] + 1,
        "slash_command_image_shown": bool(images),
    }


async def _route_entry(state: AgentState) -> str:
    """
    Graph's conditional entry point: switches to prepare_slash_command if
    the last message is a slash command whose tool name is KNOWN
    (_tools_schema_cache, nested OpenAI function-calling format
    {"function": {"name": ...}}, see mcp-client:/tools/schema) — a message
    that just starts with "/" without being a valid command (e.g. a file
    path) follows the normal flow rather than triggering a confusing 404
    error for a name that was never meant to be a tool.
    """
    parsed = _parse_slash_command(state["messages"][-1].content)
    if parsed is None:
        return "normal"
    tool_name, _ = parsed
    schema = await _get_tools_schema()
    known_names = {t.get("function", {}).get("name") for t in schema}
    return "slash_command" if tool_name in known_names else "normal"


def build_graph(checkpointer=None):
    graph = StateGraph(AgentState)
    graph.add_node("retrieve_context", retrieve_context)
    graph.add_node("select_skill", select_skill)
    graph.add_node("validate_plan", validate_plan)
    graph.add_node("revise_plan", revise_plan)
    graph.add_node("require_plan_approval", require_plan_approval)
    graph.add_node("reject_plan", reject_plan)
    graph.add_node("call_llm", call_llm)
    graph.add_node("require_approval", require_approval)
    graph.add_node("call_tools", call_tools)
    graph.add_node("auto_call_tools", auto_call_tools)
    graph.add_node("reject_tools", reject_tools)
    graph.add_node("retry_empty_answer", retry_empty_answer)
    graph.add_node("prepare_slash_command", prepare_slash_command)
    graph.add_node("run_slash_command_direct", run_slash_command_direct)

    graph.set_conditional_entry_point(
        _route_entry, {"slash_command": "prepare_slash_command", "normal": "retrieve_context"}
    )
    graph.add_conditional_edges(
        "prepare_slash_command",
        _route_slash_command_tier,
        {"sensitive": "require_approval", "direct": "run_slash_command_direct"},
    )
    graph.add_edge("run_slash_command_direct", END)
    graph.add_edge("retrieve_context", "select_skill")
    # Straight to validate_plan (the planner node that used to sit here
    # was removed, docs/resolved-bugs.md #61) — validate_plan itself
    # no-ops on an empty/absent plan, same flow as before that pipeline
    # existed.
    graph.add_edge("select_skill", "validate_plan")
    graph.add_conditional_edges(
        "validate_plan",
        route_after_validation,
        {"call_llm": "call_llm", "revise_plan": "revise_plan", "require_plan_approval": "require_plan_approval"},
    )
    graph.add_edge("revise_plan", "validate_plan")
    graph.add_conditional_edges(
        "require_plan_approval",
        route_after_plan_approval,
        {"call_llm": "call_llm", "reject_plan": "reject_plan"},
    )
    graph.add_edge("reject_plan", END)
    # has_tool_calls used directly (the verify_action passthrough node
    # that used to sit between call_llm and this routing was removed,
    # docs/resolved-bugs.md #61 — it always delegated to has_tool_calls
    # anyway).
    graph.add_conditional_edges(
        "call_llm",
        has_tool_calls,
        {
            "call_tools": "require_approval",
            "auto_call_tools": "auto_call_tools",
            "retry_empty_answer": "retry_empty_answer",
            "end": END,
        },
    )
    graph.add_conditional_edges(
        "require_approval", route_after_approval, {"call_tools": "call_tools", "reject_tools": "reject_tools"}
    )
    # call_tools/auto_call_tools/reject_tools all loop straight back to
    # call_llm (the replan/give_up/finalize routing that used to live
    # here was removed along with verify_action, its sole source of an
    # "echoue" subtask or a report_and_act-only turn — docs/resolved-bugs.md #61).
    graph.add_edge("call_tools", "call_llm")
    graph.add_edge("auto_call_tools", "call_llm")
    graph.add_edge("reject_tools", "call_llm")
    graph.add_edge("retry_empty_answer", "call_llm")

    return graph.compile(checkpointer=checkpointer or MemorySaver())


agent_graph = build_graph()
