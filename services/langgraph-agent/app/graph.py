"""
LangGraph orchestration graph.

Flow:
  1. retrieve_context   -> queries the Context Manager (RAG / memory)
  2. select_skill        -> queries the Skill Manager to inject a relevant skill prompt
  3. plan_task            -> if PLANNER_ENABLED (Iteration 1, Phase 1 "cognitive
     core", see docs/briefs/phase-1-coeur-cognitif.md), decomposes the
     objective into JSON subtasks once per task; no-op otherwise or if
     already planned (see AgentState.plan)
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
     log (Phase 2, see app/audit_log.py) any tier other than TIER_READ.
  9. verify_action        -> if VERIFICATION_ENABLED (Iteration 2, Phase 1
     "cognitive core"), compares the turn's result to the active
     subtask's success_criterion; no-op otherwise (loops straight back to
     call_llm, as before this iteration) — see route_after_verification.
  10. replan_task | report_failure -> if a subtask is marked
     "echoue": replans (REPLAN_BUDGET budget) or reports an honest
     failure to the user (END) once that budget is exhausted.
  11. END                  -> final answer

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
from langchain_core.messages import HumanMessage, SystemMessage
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


def _repeated_strategy_feedback(tool_name: str) -> str:
    """
    Garde-fou "stratégie différente" (Itération 2, voir _execute_tool_calls) :
    la tentative précédente sur cette sous-tâche a déjà échoué la
    vérification post-action avec EXACTEMENT le même outil et les mêmes
    arguments — répéter l'identique ne peut pas donner un résultat
    différent.
    """
    return (
        f"Nouvelle tentative refusée : `{tool_name}` avec exactement les mêmes arguments qu'à la "
        "tentative précédente, déjà jugée insuffisante pour cette sous-tâche. Change de stratégie "
        "(autre outil, autres arguments, autre approche) plutôt que de répéter la même action."
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

# Planner node (Iteration 1, Phase 1 "cognitive core" — see
# docs/briefs/phase-1-coeur-cognitif.md). DEFAULT FLIPPED BACK TO false
# (EFFORT 2.4, docs/history.md "EFFORT 2 — DECISIVE MEASUREMENT"): the
# "true" default (docs/briefs/flags-du-coeur-cognitif.md) held while the
# mechanism was measured and adopted (final campaign 29/33, consistent
# with pre-cognitive-core Campaign A at 30/33), but the later decisive
# cfg1-vs-cfg8 ablation (36 runs, discriminating 5-task subset) found
# cfg1 (all 4 flags off) never losing to cfg8 (all on) at 43% less
# cumulative time for essentially identical real work — and the A1
# trajectory diagnostic plus `docs/resolved-bugs.md` #51 both found the
# mechanism actively discarding genuine progress via attempt/replan-
# budget churn on multi-page tasks, not merely costing more for the same
# result. Tests that depend on cognitive-core behavior now explicitly
# force "true" (already the pattern used by the pre-adoption tests this
# same comment used to describe, just mirrored).
PLANNER_ENABLED = os.environ.get("PLANNER_ENABLED", "false").lower() == "true"

# Post-action verification + failure budget (Iteration 2, Phase 1
# "cognitive core" — see docs/briefs/phase-1-coeur-cognitif.md). ONLY HAS
# AN EFFECT IF PLANNER_ENABLED IS ALSO ON: verification compares a
# tool-call turn's result to the ACTIVE subtask's success_criterion (see
# verify_action below) — nothing to verify without a plan. DEFAULT
# FLIPPED BACK TO false, same EFFORT 2.4 justification as PLANNER_ENABLED
# above.
VERIFICATION_ENABLED = os.environ.get("VERIFICATION_ENABLED", "false").lower() == "true"
# Attempts per subtask before marking it "echoue" (see verify_action).
SUBTASK_ATTEMPT_BUDGET = int(os.environ.get("SUBTASK_ATTEMPT_BUDGET", "3"))
# Replans tolerated for a single task before honestly giving up (see
# replan_task/report_failure) rather than looping forever or claiming a
# false success.
REPLAN_BUDGET = int(os.environ.get("REPLAN_BUDGET", "2"))

# Plan validation pipeline (Iteration 3, Phase 1 "cognitive core" — see
# docs/briefs/phase-1-coeur-cognitif.md and app/plan_validation.py). ONLY
# HAS AN EFFECT IF PLANNER_ENABLED IS ALSO ON. KEPT true, UNLIKE
# PLANNER_ENABLED/VERIFICATION_ENABLED/PLAN_JUDGE_ENABLED above (EFFORT
# 2.4 safety-value exception: a programmatic heuristic gate, not a
# score-driven mechanism — untouched by the CuP reading that justified
# flipping the other three back to false).
PLAN_VALIDATION_ENABLED = os.environ.get("PLAN_VALIDATION_ENABLED", "true").lower() == "true"
# LLM judge of the plan (heuristics already passed, costly — one LLM call
# per validation). WITHDRAWAL CLAUSE (Iteration 3 brief) measured under
# real conditions (see docs/history.md, Iteration 3): it did really veto a
# plan the heuristics let through, for semantic reasons beyond their
# reach (proof of real usefulness, not a "theater" validator), at the
# cost of noticeable latency. DEFAULT FLIPPED BACK TO false (EFFORT 2.4):
# only has an effect if PLANNER_ENABLED is also true, which is now false
# by default — same justification as PLANNER_ENABLED/VERIFICATION_ENABLED
# above.
PLAN_JUDGE_ENABLED = os.environ.get("PLAN_JUDGE_ENABLED", "false").lower() == "true"
# "Justified rejection → back to the planner, max 2 cycles then human
# escalation" (brief): number of rejections (heuristics OR judge)
# tolerated before a human decides (require_plan_approval, with the
# rejection reasons displayed) rather than letting the planner loop
# indefinitely.
PLAN_VALIDATION_CYCLES_MAX = 2

# Effort 2 point 3 (docs/briefs/update-plan.md, "2.1 addendum") — 5th
# cognitive-core condition: planning as an action in the main turn
# (manage_plan tool below) instead of the 4 flags' dedicated nodes,
# targeting the auxiliary-call latency the 4-flag ablation attributed to
# plan_task/revise_plan/replan_task/the plan judge. Value-selected mode
# (like IMAGE_FORMAT_PASSTHROUGH above), not a plain on/off gate: this is
# the first 2-way string mode in this file rather than a boolean. Default
# "nodes" = current behavior, byte-for-byte unchanged. The only validated
# combination for "merged" is with the 4 flags above all "false" (asserted
# at the campaign level via campaign_preflight.py's
# CAMPAIGN_EXPECTED_FLAGS_OVERRIDE, never silently forced here) — this
# keeps plan_task/validate_plan/revise_plan/replan_task/verify_action
# structurally no-op (they already gate on those 4 flags), so all
# planning responsibility moves into manage_plan alone.
PLANNING_MODE = os.environ.get("PLANNING_MODE", "nodes")

# Qwen3.6 reasons by default on every turn (extended thinking tags) —
# useful for an initial decision, costly in latency/tokens for a fast
# perception-action loop (capture -> click -> capture...) where each
# turn only has to decide "where to click next" without reconsidering
# the whole task. If ADAPTIVE_THINKING is enabled, /no_think is injected
# (transient system prompt, not persisted — see
# _apply_adaptive_thinking) when ALL of the previous turn's tool_calls
# were auto-approved (same per-tier policy as has_tool_calls, see
# approval_policy.py); normal thinking stays active for a task's first
# turn or as soon as a sensitive tool is involved, where reasoning has
# the most value.
ADAPTIVE_THINKING = os.environ.get("ADAPTIVE_THINKING", "false").lower() == "true"
NO_THINK_DIRECTIVE = "/no_think"

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
# Cutoff date: NO official date published in the local model card
# (models/qwen3.6-27b-exl3-3.50bpw/README.md, checked — no "knowledge
# cutoff" mention). The model card places Qwen3.6's release after "the
# February release of the Qwen3.5 series" and cites AIME 2026 issues in
# its benchmarks — but the empirical observation above (Python 3.13
# claimed as the latest version when 3.14 already exists) shows the
# model's actual knowledge is older than its announced release date (a
# common case: training data freezes months before release).
# CONSERVATIVE bound chosen: don't trust volatile versions/facts without
# checking, whatever the assumed date.
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
    PEREMPTION_DIRECTIVE, before _verification_directive's per-turn
    verification instruction, which is even more volatile) — maximizes
    the length of the prefix that's actually stable from one turn to the
    next within the same day.
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


# Post-action verification merged into the tool turn itself rather than
# a separate LLM call (history of the 3 successive versions — text
# marker, dedicated tool call, current merge — in docs/history.md,
# "latency fix"). constat_precedent becomes a REQUIRED parameter of the
# schema of EVERY real tool (_inject_constat_param, _get_bound_llm): a
# single call carries both the action and its observation of the
# previous action. report_and_act remains the fallback for the sole case
# with no real action (plain-text reply, nothing to hang
# constat_precedent on).
#
# Two verified server constraints motivate this choice: JSON-schema
# constrained generation (response_format) runs with
# eos_after_completed=True (backends/exllamav3/grammar.py of the
# installed image) — it stops generation as soon as the JSON closes,
# ruling out any extra tool_calls/text in the SAME turn, confirmed by a
# real call (`tool_calls: null`). And this backend otherwise applies no
# grammar constraint on the tool_calls themselves (no filter for
# `tools`/`tool_choice`, including `tool_choice="required"`, read in
# endpoints/OAI/utils/chat_completion.py) — "required" in the JSON schema
# is therefore not grammatically enforced; reliability comes from
# merging into a single call, not from a grammar guarantee. Hence the
# permanent coverage judge (see verify_action): to be measured, not
# assumed.
_CONSTAT_PARAM_NAME = "constat_precedent"
_CONSTAT_VERDICTS = ("atteint", "non_atteint", "sans_objet")
# Trimmed down (the "post-1/2-ter arbitration" fix, see docs/history.md):
# a bare enum, no description — measured at +6,931 tokens/turn (+65%)
# with the description repeated across the 64 real tools vs. +2,569
# tokens (+24%) without it (TabbyAPI's real tokenizer, /v1/token/encode).
# The semantics only need explaining ONCE: it lives in
# _verification_directive (injected into the system prompt on every
# turn, a single copy), not in the schema of EVERY tool. Protected by the
# coverage judge (verify_action/constats_inexploitables): if this
# trimming drops real coverage below 95%, the description comes back.
_CONSTAT_PARAM_SCHEMA = {
    "type": "string",
    "enum": list(_CONSTAT_VERDICTS),
}


def _inject_constat_param(tool: dict) -> dict:
    """
    Augments a real MCP tool's OpenAI function-calling schema with the
    required constat_precedent parameter (see above) — a copy, never
    mutates the original schema (shared via _tools_schema_cache). Removed
    from the arguments BEFORE any real dispatch (see
    _execute_tool_calls): the MCP servers themselves don't know about it.
    """
    fn = tool.get("function", {})
    params = dict(fn.get("parameters") or {"type": "object", "properties": {}})
    properties = dict(params.get("properties") or {})
    properties[_CONSTAT_PARAM_NAME] = _CONSTAT_PARAM_SCHEMA
    required = list(params.get("required") or [])
    if _CONSTAT_PARAM_NAME not in required:
        required.append(_CONSTAT_PARAM_NAME)
    return {**tool, "function": {**fn, "parameters": {**params, "properties": properties, "required": required}}}


# Name shared with approval_policy.REPORT_AND_ACT_TOOL_NAME (source of
# truth for tiering, see tool_tier()) — duplicated here as a local
# constant rather than re-imported on every use, purely for the
# readability of the many references below; MUST stay identical.
_REPORT_AND_ACT_TOOL_NAME = approval_policy.REPORT_AND_ACT_TOOL_NAME
_REPORT_AND_ACT_TOOL = {
    "type": "function",
    "function": {
        "name": _REPORT_AND_ACT_TOOL_NAME,
        "description": (
            "Outil de repli, à appeler UNIQUEMENT quand tu réponds en texte "
            "pur ce tour-ci, sans appeler aucun autre outil (ex. réponse "
            "finale) : constate si l'action PRÉCÉDENTE a atteint son "
            "critère. Si tu appelles un AUTRE outil ce tour-ci, mets plutôt "
            "constat_precedent directement dans SES arguments — n'appelle "
            "jamais les deux à la fois."
        ),
        "parameters": {
            "type": "object",
            "properties": {_CONSTAT_PARAM_NAME: _CONSTAT_PARAM_SCHEMA},
            "required": [_CONSTAT_PARAM_NAME],
        },
    },
}


# Merged-planning mode (PLANNING_MODE="merged", effort 2 point 3, see
# docs/briefs/update-plan.md "2.1 addendum"): same non-MCP, graph-only
# precedent as _REPORT_AND_ACT_TOOL above — dispatched locally in
# _execute_tool_calls, never sent to mcp-client. Only exposed by
# _get_bound_llm when PLANNING_MODE == "merged". Two actions, deliberately
# no third "fail"/"replan" action: a stuck subtask is handled by calling
# set_plan again (replacing the remaining subtasks) rather than by ever
# persisting an "echoue" status — that status is what would route to the
# costly replan_task node in the 4-flag architecture (route_after_tool_
# execution), exactly the auxiliary call this mode exists to remove.
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


def _parse_constat(tool_calls: Optional[list]) -> tuple[Optional[str], bool]:
    """
    Looks for constat_precedent among the turn's tool_calls — either on
    report_and_act (priority, "plain text, no action" case), or on the
    first real tool_call carrying it (normal, merged case). Returns
    (verdict, exploitable):
    - (None, False) if absent from all tool_calls, or found but outside
      the enum (malformed);
    - (verdict, True) otherwise, verdict ∈ _CONSTAT_VERDICTS.
    "exploitable=False" drives the constats_inexploitables counter and
    the coverage judge (verify_action) — distinct from a "sans_objet"
    legitimately declared by the model (exploitable=True in that case).
    """
    report_call = next((tc for tc in tool_calls or [] if tc.get("name") == _REPORT_AND_ACT_TOOL_NAME), None)
    if report_call is not None:
        verdict = (report_call.get("args") or {}).get(_CONSTAT_PARAM_NAME)
        return (verdict, True) if verdict in _CONSTAT_VERDICTS else (None, False)
    for tc in tool_calls or []:
        args = tc.get("args") or {}
        if _CONSTAT_PARAM_NAME in args:
            verdict = args.get(_CONSTAT_PARAM_NAME)
            return (verdict, True) if verdict in _CONSTAT_VERDICTS else (None, False)
    return None, False


# Planner node (Iteration 1, see plan_task below): recognizes a possible
# ```json ... ``` / ``` ... ``` wrapper around the reply — the model may
# wrap the JSON despite the raw-output instruction, as already observed
# for other output formats in this file (see _extract_fallback_tool_call
# above).
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


class PlanJudgeValidationError(ValueError):
    """Raised by _validate_judge_json: the plan judge's verdict is unusable."""


def _validate_judge_json(raw: str) -> dict:
    """
    Schema validated PROGRAMMATICALLY (Iteration 3, same pipeline as
    _validate_plan_json): requires {"faisable": bool},
    "risques"/"etapes_manquantes" optional (falls back to an empty list
    if absent/malformed — accessories for visibility, not for the
    decision).
    """
    text = _strip_code_fence(_THINK_BLOCK_RE.sub("", raw or ""))
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlanJudgeValidationError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("faisable"), bool):
        raise PlanJudgeValidationError("'faisable' key (bool) missing or invalid")
    risques = data.get("risques")
    etapes = data.get("etapes_manquantes")
    return {
        "faisable": data["faisable"],
        "risques": [r for r in risques if isinstance(r, str)] if isinstance(risques, list) else [],
        "etapes_manquantes": [e for e in etapes if isinstance(e, str)] if isinstance(etapes, list) else [],
    }


PLAN_JUDGE_SYSTEM_PROMPT = (
    "Tu es le juge d'un agent qui accomplit des tâches web. On te donne un "
    "objectif, un plan (liste de sous-tâches avec critère de succès et "
    "outils prévus), et SI DISPONIBLE l'état ACTUEL de la page déjà visitée "
    "pour cette tâche (etat_actuel_de_la_page). Évalue s'il est réellement "
    "faisable et complet pour atteindre l'objectif. Si un état de page est "
    "fourni, base ton jugement sur ce qui existe RÉELLEMENT dessus (ex. ne "
    "reproche jamais l'absence d'une barre de recherche ou d'une "
    "fonctionnalité qui n'apparaît pas dans l'état fourni). Vérifie aussi "
    "que le plan cible bien l'élément EXACT demandé par l'objectif (ex. une "
    "référence précise) et ne l'a pas substitué par un élément différent "
    "simplement parce qu'il apparaît sur la page — rejette un plan qui "
    "ferait cette confusion. Réponds "
    'UNIQUEMENT par un JSON de la forme {"faisable": true|false, "risques": '
    '["..."], "etapes_manquantes": ["..."]}, rien d\'autre : pas de texte '
    "avant/après, pas de balise <think>, pas de bloc de code."
)


async def _judge_plan(plan: list, objective: str, page_snapshot: Optional[str] = None) -> list:
    """
    LLM judge verdict (Iteration 3, page_snapshot added in Iteration 4 —
    grounding fix, see docs/history.md): list of rejection reasons (empty
    = feasible). Degrades to FAIL-OPEN on LLM error/invalid JSON (no
    reason returned, no veto by default) — consistent with the brief's
    "never an infinite loop": an unavailable judge must never
    indefinitely block a task that's otherwise valid per the heuristics.
    """
    payload = json.dumps(
        {
            "objectif": objective,
            "plan": [
                {
                    "description": st.get("description", ""),
                    "critere_succes": st.get("success_criterion", ""),
                    "outils": st.get("tools", []),
                }
                for st in plan
            ],
            "etat_actuel_de_la_page": page_snapshot,
        },
        ensure_ascii=False,
    )
    try:
        response = await planner_llm.ainvoke([SystemMessage(content=PLAN_JUDGE_SYSTEM_PROMPT), HumanMessage(content=payload)])
        verdict = _validate_judge_json(response.content)
    except Exception:
        logger.warning("Juge de plan indisponible, aucun veto appliqué par défaut.", exc_info=True)
        return []
    if verdict["faisable"]:
        return []
    reasons = [f"juge : {r}" for r in verdict["risques"]] or ["juge : plan jugé non faisable"]
    if verdict["etapes_manquantes"]:
        reasons.append("juge : étapes manquantes — " + "; ".join(verdict["etapes_manquantes"]))
    return reasons


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
    Snapshot of the current page to ground a (re)planning/validation step
    in what ACTUALLY exists (Iteration 4, continuation of the
    verify_action fix — see docs/history.md). `None` if no navigation has
    happened yet for this task (state["current_page_url"], Phase 1): the
    VERY FIRST plan (plan_task) therefore stays structurally ungrounded —
    no page exists yet to capture at that point, and forcing an
    exploratory navigation before planning would raise its own tier/
    approval questions (browser_navigate is TIER_SENSITIVE), out of scope
    here. REPLANNING (revise_plan/replan_task), on the other hand, is
    always triggered AFTER a navigation has happened — that's where this
    fix applies.
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
    # docs/briefs/phase-1-coeur-cognitif.md and plan_task below): list of
    # {description, success_criterion, status, attempts, result}.
    # status ∈ {"a_faire", "en_cours", "fait", "echoue"} (free string, no
    # dedicated enum — consistent with failure_cause in the test harness).
    # Computed ONCE by plan_task at the very start of a task (empty list ->
    # the planner runs; non-empty -> passthrough, never rebuilt within the
    # same task). Reset to [] on every NEW top-level user message (see
    # run_input, app/main.py), like observed_urls. No validation/tier/
    # post-action verification wired to it yet (Iterations 2/3 to come):
    # structure and visibility only in Iteration 1; post-action
    # verification/failure budget wired to it since Iteration 2 (see
    # verify_action, replan_task, report_failure below). No-op while
    # PLANNER_ENABLED is disabled (default): stays [] then.
    plan: list
    # Episode compaction (Phase 2, PLAN.md): subtask_message_start[i] is
    # len(messages) at the moment plan[i] became "en_cours" — lets
    # _apply_episode_compaction find each completed subtask's raw message
    # range without scanning message content. Set by plan_task/revise_plan
    # (fresh list, index 0) and verify_action (appended on advance);
    # replan_task keeps entries before the replanned index, resets the
    # replanned one. Reset to [] on every new top-level user message (see
    # run_input, app/main.py), same lifecycle as plan.
    subtask_message_start: list
    # Number of replans already performed for THIS task (Iteration 2, see
    # replan_task/route_after_verification) — cumulative budget, like
    # tool_iterations, capped by REPLAN_BUDGET. Reset to 0 on every new
    # top-level user message (see run_input, app/main.py).
    replan_count: int
    # Plan validation pipeline (Iteration 3, see validate_plan/
    # revise_plan/require_plan_approval below). plan_validation_reasons:
    # reasons for the LAST rejection (heuristics and/or judge), [] if the
    # current plan is valid (or not yet evaluated). plan_validation_cycles:
    # number of rejections suffered for THIS task (not per proposed plan —
    # a budget shared between initial planning and replans, see
    # PLAN_VALIDATION_CYCLES_MAX), beyond which human escalation kicks in
    # rather than looping indefinitely on the planner. Both reset to
    # zero/empty on every new top-level user message (see run_input,
    # app/main.py).
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
    # True as soon as an action has just been executed
    # (_execute_tool_calls), consumed by verify_action on the next turn —
    # more robust than searching the history for the last tool_call:
    # without this explicit marker, a replan turn (which executes NO tool)
    # could be mistaken for an action still awaiting verification if the
    # previous turn had no tool_calls.
    pending_verification: bool
    # Cumulative counter (reset to 0 on every new top-level user message,
    # run_input/app/main.py), incremented when pending_verification was
    # true but no usable observation could be extracted from the turn.
    # DELIBERATELY separate degradation path: this case is MEASURED (a
    # dedicated metric) rather than BILLED as a subtask failure (see
    # verify_action and docs/history.md, "latency fix", for the score broken
    # by the old mechanism that counted it as a failure).
    constats_inexploitables: int


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
# live campaign (see docs/history.md): auxiliary LLM calls (plan_task/
# revise_plan/verify_action/_judge_plan) used `llm` above, capped at
# LLM_MAX_TOKENS (2048, sized for the main conversational turn).
# Qwen3.6/TabbyAPI reasons in a reasoning_content field SEPARATE from
# content before answering (confirmed via a direct non-streaming call to
# TabbyAPI); this reasoning, often long, consumed the whole budget on its
# own, truncating `content` to empty or mid-JSON (finish_reason="length")
# — every validator then systematically fell back to its error path,
# never a real evaluation. `/no_think` as a prompt prefix (the existing
# ADAPTIVE_THINKING mechanism) does NOT suppress reasoning on this backend
# (verified by the same direct call) — solution adopted: a more generous
# token budget, dedicated to these structured calls, separate from the
# main loop's budget (whose small value remains an intentional safety net
# against repetition drift, see LLM_MAX_TOKENS).
PLANNER_MAX_TOKENS = int(os.environ.get("PLANNER_MAX_TOKENS", "8192"))
# Thinking curbed on auxiliary calls (plan_task/revise_plan/
# replan_task/_judge_plan, all via planner_llm) — unlike `/no_think` as a
# prompt prefix (ADAPTIVE_THINKING, confirmed to have no effect on this
# backend, see comment above), TabbyAPI exposes a real PER-REQUEST
# server-side parameter (`GET /openapi.json`, ChatCompletionRequest
# schema: `enable_thinking: bool`), verified LIVE before writing this fix
# (real call with a JSON planning prompt, see docs/history.md):
# `reasoning_content: null`, immediate valid JSON, no reasoning.
# `extra_body` is a native langchain-openai parameter (verified:
# `"extra_body" in inspect.signature(ChatOpenAI).parameters`).
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


async def _get_tools_schema() -> list:
    """Fills/returns _tools_schema_cache — factored out of _get_bound_llm
    so it's also usable by _route_entry (validating a slash command's tool
    name) without an extra HTTP request once cached."""
    global _tools_schema_cache
    if _tools_schema_cache is None:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{MCP_CLIENT_URL}/tools/schema")
                resp.raise_for_status()
                _tools_schema_cache = resp.json().get("tools", [])
        except (httpx.HTTPError, ValueError):
            # mcp-client unreachable or invalid response: degrade with no
            # tools rather than failing the whole conversation.
            _tools_schema_cache = []
    return _tools_schema_cache


async def _get_bound_llm() -> ChatOpenAI:
    schema = await _get_tools_schema()
    # Synthetic, non-MCP tool (independent of VERIFICATION_ENABLED — merged
    # mode manages its own plan state instead of the constat_precedent/
    # report_and_act self-report pattern below), see PLANNING_MODE above.
    extra_tools = [_MANAGE_PLAN_TOOL] if PLANNING_MODE == "merged" else []
    if not schema:
        return llm.bind_tools(extra_tools) if extra_tools else llm
    if not VERIFICATION_ENABLED:
        # extra_tools FIRST (correction 2/2, fifth-condition diagnostic,
        # see docs/history.md "EFFORT 2" point 3): manage_plan previously
        # sat last, after the full ~63-64 MCP/browser catalog — the one
        # variable left untried after cause 3's fix (persistent plan
        # section) still measured merged_plan_calls=0. No-op outside
        # merged mode (extra_tools == [], list identity unchanged).
        return llm.bind_tools(extra_tools + schema)
    # constat_precedent injected as a required parameter of EVERY real MCP
    # tool, plus report_and_act as the sole fallback (pure-text turn, no
    # action) — see above. Gated on VERIFICATION_ENABLED: without it, this
    # field has no reader (_verification_directive doesn't instruct it)
    # and would only add noise to the schema sent to the model.
    wrapped = [_inject_constat_param(t) for t in schema]
    return llm.bind_tools(wrapped + [_REPORT_AND_ACT_TOOL] + extra_tools)


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
    between tasks. Used by plan_task/revise_plan/replan_task.
    """
    schema = await _get_tools_schema()
    names = sorted({t.get("function", {}).get("name") for t in schema} - {None})
    if not names:
        return ""
    return (
        "\n\nOutils réellement disponibles (utilise UNIQUEMENT ces noms exacts "
        'dans "outils", liste vide si aucun ne s\'applique) : ' + ", ".join(names)
    )


async def plan_task(state: AgentState, config: dict) -> dict:
    """
    Planner node (Iteration 1, Phase 1 "cognitive core"). No-op
    (`{"messages": []}`) if PLANNER_ENABLED is disabled (default), if a
    plan already exists for this task (computed once, never rebuilt
    within the same task — see AgentState.plan), or if there's no human
    message to plan from.

    LLM call kept separate from call_llm: raw `llm` (never `bound_llm`),
    the planner must never emit tool_calls, only JSON.

    ALWAYS degrades to a single-subtask plan rather than blocking the task
    over a side planning issue (HTTP transport, invalid response) —
    deliberately broad catch (PlanValidationError or any OpenAI client/
    httpx error), same spirit as the httpx.HTTPError degradation in
    retrieve_context/select_skill above, widened here since the failure
    can also come from JSON validation, not just transport.

    Logs a role="planning" audit entry (coverage counter, symmetric to
    verify_action's role="verification" — see docs/history.md, EFFORT 2
    "judge validity check": archives had no way to tell whether the
    planner ever produced a non-trivial plan, only whether it was
    enabled) with the initial subtask count and a `trivial` flag
    (1-subtask plan = the planner had no effect on task structure).
    """
    if not PLANNER_ENABLED or state.get("plan"):
        return {"messages": []}
    first_human = next((m for m in state["messages"] if getattr(m, "type", None) == "human"), None)
    objective = first_human.content if first_human and isinstance(first_human.content, str) else ""
    if not objective:
        return {"messages": []}

    try:
        tools_hint = await _available_tools_hint()
        response = await planner_llm.ainvoke(
            [SystemMessage(content=PLANNER_SYSTEM_PROMPT), HumanMessage(content=objective + tools_hint)]
        )
        subtasks = _validate_plan_json(response.content)
    except Exception:
        logger.warning("Planning failed, falling back to a single-subtask plan.", exc_info=True)
        subtasks = [{"description": objective, "success_criterion": "objectif de la tâche atteint", "tools": []}]

    plan = [{**st, "status": "a_faire", "attempts": 0, "result": None} for st in subtasks]
    if plan:
        plan[0]["status"] = "en_cours"
    logger.info("Initial plan (%d subtask(s)): %s", len(plan), plan)
    thread_id = config.get("configurable", {}).get("thread_id", "")
    audit_log.log_message(thread_id, "planning", {"subtask_count": len(plan), "trivial": len(plan) <= 1})
    return {"plan": plan, "subtask_message_start": [len(state["messages"])] if plan else []}


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
    Plan validation pipeline (Iteration 3, Phase 1 "cognitive core").
    No-op (`{"messages": []}`) if PLAN_VALIDATION_ENABLED is disabled
    (default) or if `state["plan"]` is empty — same behavior as before
    this iteration. Otherwise: programmatic heuristics
    (app/plan_validation.py, free) then, ONLY if they pass AND
    PLAN_JUDGE_ENABLED, LLM judge (costly — withdrawal clause, see
    docs/history.md). Rejection (heuristics OR judge) -> plan_validation_cycles
    incremented, reasons returned for route_after_validation.

    Logs a role="plan_validation" audit entry (coverage counter,
    docs/history.md EFFORT 2 "judge validity check"): heuristic rejection
    and judge invocation/veto are distinct signals, kept separate rather
    than collapsed into the single `reasons` list used for routing —
    "the judge never fired" and "the judge fired and approved" were
    previously indistinguishable from archives alone.
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

    judge_invoked = False
    judge_reasons = []
    if not heuristic_reasons and PLAN_JUDGE_ENABLED:
        judge_invoked = True
        first_human = next((m for m in state["messages"] if getattr(m, "type", None) == "human"), None)
        objective = first_human.content if first_human and isinstance(first_human.content, str) else ""
        page_snapshot = await _grounding_snapshot(state, objective)
        judge_reasons = await _judge_plan(plan, objective, page_snapshot)

    reasons = heuristic_reasons or judge_reasons
    thread_id = config.get("configurable", {}).get("thread_id", "")
    audit_log.log_message(
        thread_id,
        "plan_validation",
        {
            "heuristic_rejected": bool(heuristic_reasons),
            "judge_invoked": judge_invoked,
            "judge_vetoed": bool(judge_reasons),
        },
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
    (Iteration 3). Distinct from replan_task (Iteration 2, triggered by a
    subtask EXECUTION FAILURE): here, nothing has been executed yet — the
    plan itself is judged structurally/semantically insufficient BEFORE
    the first turn. Regenerates the WHOLE plan (no "done" subtask to
    preserve) with the rejection reasons as context. Same fallback as
    plan_task on generation failure (single-subtask plan).
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


def _summarize_subtask(subtask: dict, turns: list) -> str:
    """Structured summary replacing a completed subtask's raw turns (see
    _apply_episode_compaction): description, key actions distilled from
    the AI messages' tool_calls in that range (name + first argument
    value, truncated), and the result verify_action recorded."""
    actions = []
    for m in turns:
        for call in getattr(m, "tool_calls", None) or []:
            args = call.get("args") or {}
            hint = str(next(iter(args.values()), ""))[:40]
            actions.append(f"{call.get('name', '?')}({hint})" if hint else call.get("name", "?"))
    result = subtask.get("result") or "(résultat non consigné)"
    return (
        f"[Sous-tâche compactée] {subtask.get('description', '')} — "
        f"actions : {', '.join(actions) or '(aucune)'} — résultat : {result}"
    )


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


def _previous_turn_tool_calls(messages: list) -> Optional[list]:
    """Last AI message with tool_calls in the history — the turn that led to this call_llm invocation."""
    for message in reversed(messages):
        if getattr(message, "type", None) == "ai" and getattr(message, "tool_calls", None):
            return message.tool_calls
    return None


def _apply_adaptive_thinking(messages: list, session_grants) -> list:
    """
    Adds a transient "/no_think" system prompt (never persisted in graph
    state, see _apply_image_retention for the same principle) when
    ADAPTIVE_THINKING is enabled AND the previous turn was fully
    auto-approved (same tier policy as has_tool_calls) — typically a
    repeated read/reversible tool loop where Qwen3.6's extended reasoning
    costs more than it's worth. No injection
    on a task's very first turn (no previous tool_calls) nor as soon as a
    sensitive tool was involved: reasoning has the most value there.
    """
    if not ADAPTIVE_THINKING:
        return messages
    previous_tool_calls = _previous_turn_tool_calls(messages)
    if not previous_tool_calls:
        return messages
    all_auto_approved = all(
        approval_policy.is_auto_approved(tc["name"], tc.get("args"), session_grants)
        for tc in previous_tool_calls
    )
    if not all_auto_approved:
        return messages
    # Merged into the leading system message if there is one (real case:
    # DOWNLOAD_DIRECTIVE and friends, added by call_llm right before this
    # call), otherwise inserted at position 0 — never at the end of the list:
    # some backends (TabbyAPI/ExLlamaV3, Qwen3.6's strict Jinja template)
    # explicitly reject a second system message or one not at the head
    # ("TemplateError: System message must be at the beginning") —
    # llama-server/Ollama tolerate both forms, so this bug stayed
    # invisible before the migration to TabbyAPI.
    if messages and isinstance(messages[0], SystemMessage):
        head, *rest = messages
        merged_head = SystemMessage(content=f"{head.content}\n{NO_THINK_DIRECTIVE}")
        return [merged_head] + rest
    return [SystemMessage(content=NO_THINK_DIRECTIVE)] + messages


def _verification_directive(state: AgentState) -> str:
    """
    Injects the observation on the previous action into the current
    turn's reasoning rather than a separate LLM call (history in
    docs/history.md, "latency fix") — near-zero marginal cost. The base
    reminder (constat_precedent required on EVERY tool_call,
    _inject_constat_param in _get_bound_llm) is ALWAYS injected as soon as
    VERIFICATION_ENABLED is active, from the task's very first tool call
    onward (nothing to observe yet -> "sans_objet"). The SPECIFIC hint
    (active subtask's criterion) stays conditioned on
    `pending_verification` + subtask "en_cours" (MUST stay in sync with
    verify_action): nothing new to observe otherwise (e.g. a replan turn,
    which executes no tool).
    """
    if not VERIFICATION_ENABLED:
        return ""
    base = (
        "\nchaque appel d'outil doit inclure constat_precedent (atteint / "
        "non_atteint / sans_objet) sur l'action PRÉCÉDENTE — sans_objet "
        "s'il n'y a rien à constater (ex. toute première action de la "
        "tâche). Si tu réponds en texte pur ce tour-ci sans appeler "
        "d'autre outil, appelle report_and_act à la place, jamais les deux."
    )
    if not state.get("pending_verification"):
        return base
    plan = state.get("plan") or []
    active_index = _active_subtask_index(plan)
    if active_index is None:
        return base
    critere = plan[active_index]["success_criterion"]
    return base + (
        f' Ce tour-ci : l\'action précédente a-t-elle atteint son critère "{critere}" '
        "? Juge sur le résultat d'outil ci-dessus (pas sur le critère seul "
        "— s'il suppose une approche qui n'existe pas réellement sur cette "
        "page, juge la progression réelle)."
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
                # that prefix cacheable, same reasoning as
                # _verification_directive's position (mutually exclusive
                # with this mode, always "" here — see its docstring).
                # An earlier version put it FIRST for primacy (see
                # docs/history.md, EFFORT 2 point 3): superseded by the
                # persistent-section redesign, not stacked with it.
                f"{DOWNLOAD_DIRECTIVE}{BULK_CHECK_DIRECTIVE}{PEREMPTION_DIRECTIVE}"
                f"{_date_directive()}{_verification_directive(state)}{_merged_plan_directive(state)}"
            )
        )
    ] + compacted_messages
    messages_for_llm = _apply_image_retention(messages_for_llm)
    messages_for_llm = _apply_adaptive_thinking(messages_for_llm, state.get("session_grants") or [])
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
    # (log_tool_call), deliberately partial by tier, this trace doesn't
    # need to be selective: it's the agent's reasoning, never a side
    # effect to filter.
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


async def _call_mcp_tool(
    client: httpx.AsyncClient, tool_name: str, args: dict, thread_id: Optional[str] = None
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
    """
    try:
        resp = await client.post(
            f"{MCP_CLIENT_URL}/call",
            json={"tool": tool_name, "arguments": args, "thread_id": thread_id},
        )
        resp.raise_for_status()
        result = resp.json()
    except httpx.HTTPError as exc:
        return {"error": str(exc)}, []
    return _split_image_blocks(result)


async def _execute_tool_calls(state: AgentState, config: dict) -> dict:
    """
    Logic shared between call_tools (reached after require_approval) and
    auto_call_tools (reached directly from has_tool_calls, never seen by
    a human THIS turn). Logs (app/audit_log.py) any tool_call whose
    effective tier isn't TIER_READ (silent by design, nothing new to
    trace) — including those coming from call_tools, whatever their tier.

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

    # "Different strategy" guardrail (Iteration 2, see
    # _repeated_strategy_feedback): applies ONLY if a verification failure
    # has already been observed on the active subtask (attempts > 0) — a
    # very first attempt has nothing to repeat. Comparison by strict
    # name+args equality (no generic ε tolerance on arbitrary argument
    # schemas — an accepted simplification).
    plan = state.get("plan") or []
    active_index = _active_subtask_index(plan)
    active_attempts = plan[active_index].get("attempts", 0) if active_index is not None else 0
    # Merged-planning mode only (PLANNING_MODE="merged", see manage_plan
    # dispatch below): tracks whether this turn's tool_calls actually
    # mutated the plan, so the returned dict only includes "plan"/
    # "subtask_message_start" when there's something new to report —
    # every other mode's return shape stays byte-for-byte unchanged.
    plan_changed = False
    subtask_message_start = state.get("subtask_message_start") or []
    # state["messages"][-1] IS `last`, the CURRENT turn whose tool_calls
    # are being executed — excluded from the search (messages[:-1]) so
    # that "previous_tool_calls" truly refers to the PREVIOUS turn, not
    # this one (otherwise any tool_call would compare against itself).
    previous_tool_calls = (
        (_previous_turn_tool_calls(state["messages"][:-1]) or []) if VERIFICATION_ENABLED else []
    )

    async with httpx.AsyncClient(timeout=60) as client:
        for tool_call in last.tool_calls:
            if tool_call["name"] == _REPORT_AND_ACT_TOOL_NAME:
                # Fallback meta-tool (latency fix 1/2-ter, see
                # _parse_constat): already consumed by verify_action (runs
                # BEFORE this node, on the same AIMessage) to mutate the
                # plan — never dispatched to mcp-client (it's not a real
                # MCP tool), never audited (TIER_READ, see
                # approval_policy._DEFAULT_TIER_READ). An acknowledgment
                # ToolMessage is still mandatory: every tool_call from the
                # previous AIMessage must have its response, otherwise the
                # next LLM call would break the OpenAI format.
                new_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": json.dumps({"ok": True}, ensure_ascii=False),
                    }
                )
                continue

            if tool_call["name"] == _MANAGE_PLAN_TOOL_NAME:
                # Merged-planning mode's entire planning/replanning/
                # verification responsibility (PLANNING_MODE="merged",
                # docs/briefs/update-plan.md "2.1 addendum") — never
                # dispatched to mcp-client, mutates `plan` synchronously,
                # same "no dedicated LLM call" property as report_and_act
                # above. TIER_READ (approval_policy.tool_tier), so this
                # never reaches require_approval.
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

            # constat_precedent travels in the real tool's own arguments
            # (augmented schema, see _inject_constat_param) — stripped
            # HERE, before any use of tool_call["args"] below (mcp-client
            # dispatch, anti-fabrication guardrail, anti-repetition
            # comparison, audit). Without this stripping, the
            # anti-repetition guardrail's strict name+args comparison
            # (below) would NEVER again match two otherwise identical
            # attempts (a different constat each time) — silently
            # disabling that guardrail.
            if _CONSTAT_PARAM_NAME in (tool_call.get("args") or {}):
                tool_call = {
                    **tool_call,
                    "args": {k: v for k, v in tool_call["args"].items() if k != _CONSTAT_PARAM_NAME},
                }

            tier = approval_policy.effective_tier(tool_call["name"], tool_call.get("args"), grants)
            audit_tier = tier if tier != approval_policy.TIER_READ else None

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
            elif (
                VERIFICATION_ENABLED
                and active_attempts > 0
                and any(
                    tc.get("name") == tool_call["name"] and tc.get("args") == tool_call.get("args")
                    for tc in previous_tool_calls
                )
            ):
                blocked = True
                result = {"content": [{"type": "text", "text": _repeated_strategy_feedback(tool_call["name"])}]}
                images = []
            else:
                result, images = await _call_mcp_tool(client, tool_call["name"], tool_call["args"], thread_id)
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

            if audit_tier is not None:
                # Logged AFTER execution (see above) to carry the result
                # as seen by the model (already truncated/prioritized
                # above if browser_*) — see app/audit_log.py, "revised
                # Phase 1d".
                audit_log.log_tool_call(thread_id, tool_call["name"], tool_call["args"], audit_tier, result)

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
        # An action was just executed, verify_action has something to
        # observe on the next turn (see AgentState.pending_verification).
        "pending_verification": True,
    }
    if plan_changed:
        # Merged-planning mode only (see manage_plan dispatch above) —
        # every other mode never sets plan_changed, so this key is absent
        # from the returned dict and state["plan"] stays whatever
        # verify_action (or nothing, if PLANNER_ENABLED is off) decided.
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


_PLAN_STATUS_LABELS_GRAPH = {"a_faire": "à faire", "en_cours": "en cours", "fait": "fait", "echoue": "échoué"}


def _active_subtask_index(plan: list) -> Optional[int]:
    """Index of the plan's "en_cours" subtask, or None (none/empty plan) —
    plan invariant (Iteration 1/2): at most one "en_cours" subtask at a time."""
    return next((i for i, st in enumerate(plan) if st.get("status") == "en_cours"), None)


async def verify_action(state: AgentState, config: dict) -> dict:
    """
    Analysis of the post-action verification observation (history of
    successive revisions in docs/history.md, "latency fix" — see also
    _verification_directive above). NO LONGER MAKES AN LLM CALL: the
    verdict is parsed from the tool_calls call_llm just produced (THAT
    SAME call also observed the previous action's result AND decided the
    next step — see _verification_directive). This node only reads that
    tool call (report_and_act) and updates the plan accordingly.

    No-op (`{"messages": []}`) if VERIFICATION_ENABLED is disabled
    (default), if there's no "en_cours" subtask, or if
    `pending_verification` (AgentState) is false — same conditions as
    _verification_directive, to keep in sync: if the instruction wasn't
    injected, there's nothing to parse here either. Always consumes the
    flag (`pending_verification: False` on return): once observed, an
    action must not be re-observed on the next turn if no NEW action has
    been executed in between (e.g. a replan turn, which executes no tool).

    Criterion verified = success_criterion of the plan's ACTIVE subtask.
    DELIBERATELY REVERSED degradation (see docs/history.md, "latency fix",
    for the score broken — 18/33 — by the previous version which treated
    a missing observation as a failure): missing/malformed observation ->
    "sans_objet" (NEITHER success NOR failure, attempt budget unchanged),
    counted in constats_inexploitables rather than billed to the
    subtask. A "sans_objet" legitimately declared BY THE MODEL has the
    same effect on the plan (no mutation) but does NOT increment this
    counter — only ambiguity (missing/malformed observation) is measured.

    Every evaluation here (usable or not) logs an audit entry with
    `role="verification"` and its usability verdict — a permanent
    COVERAGE judge (usable observations / opportunities), companion to
    constats_inexploitables which only measured half the contract
    (ambiguity, not the plain absence of an attempt). Without this
    systematic counting, a campaign can show constats_inexploitables ≈ 0
    while the real coverage rate is catastrophic (~9% measured on the
    campaign that motivated this judge): verify_action only counts as
    "unusable" attempts recognized as such, never an observation that
    wasn't even attempted.
    """
    if not VERIFICATION_ENABLED:
        return {"messages": []}
    if not state.get("pending_verification"):
        return {"messages": []}
    plan = state.get("plan") or []
    active_index = _active_subtask_index(plan)
    if active_index is None:
        return {"messages": [], "pending_verification": False}

    last = state["messages"][-1]
    verdict, exploitable = _parse_constat(getattr(last, "tool_calls", None))
    thread_id = config.get("configurable", {}).get("thread_id", "")
    audit_log.log_message(thread_id, "verification", {"exploitable": exploitable, "verdict": verdict})

    if not exploitable:
        logger.warning(
            "Subtask %d: constat_precedent missing or malformed, unusable observation "
            "(sans_objet, attempt budget unchanged)",
            active_index,
        )
        return {
            "pending_verification": False,
            "constats_inexploitables": state.get("constats_inexploitables", 0) + 1,
        }

    if verdict == "sans_objet":
        logger.info("Subtask %d: sans_objet observation (nothing to update)", active_index)
        return {"pending_verification": False}

    new_plan = [dict(st) for st in plan]
    if verdict == "atteint":
        new_plan[active_index]["status"] = "fait"
        new_plan[active_index]["result"] = "critère atteint (constat intégré au tour)"
        result = {"plan": new_plan, "pending_verification": False}
        if active_index + 1 < len(new_plan):
            new_plan[active_index + 1]["status"] = "en_cours"
            boundaries = list(state.get("subtask_message_start") or [])
            if len(boundaries) == active_index + 1:
                boundaries.append(len(state["messages"]))
                result["subtask_message_start"] = boundaries
        logger.info("Subtask %d reached", active_index)
        return result

    # verdict == "non_atteint"
    attempts = new_plan[active_index]["attempts"] + 1
    new_plan[active_index]["attempts"] = attempts
    if attempts < SUBTASK_ATTEMPT_BUDGET:
        logger.info(
            "Subtask %d not reached (attempt %d/%d)",
            active_index, attempts, SUBTASK_ATTEMPT_BUDGET,
        )
        return {"plan": new_plan, "pending_verification": False}

    new_plan[active_index]["status"] = "echoue"
    new_plan[active_index]["result"] = "critère non atteint (constat intégré au tour)"
    logger.warning("Subtask %d failed after %d attempts", active_index, attempts)
    return {"plan": new_plan, "pending_verification": False}


async def replan_task(state: AgentState, config: dict) -> dict:
    """
    Replanning (Iteration 2): reached when verify_action has marked a
    subtask "echoue". Reuses PLANNER_SYSTEM_PROMPT/_validate_plan_json
    (same schema as plan_task) with a context prompt (objective, subtasks
    already "fait", failure reason). "fait" subtasks preserved as-is; the
    failed subtask and everything after it are replaced by the new
    breakdown. Replanning failure (LLM/invalid JSON): falls back WITHOUT
    raising — just resets the failed subtask to "en_cours"/attempts=0 (a
    new chance on the SAME plan rather than crashing). replan_count
    incremented in all cases (budget consumed even if the replanning
    itself fails).

    Logs a role="replanning" audit entry (coverage counter, docs/history.md
    EFFORT 2 "judge validity check") for every REAL replan (failed_index
    found) — the defensive early return below (no failed subtask, should
    not normally happen) consumes budget but changes nothing, so it stays
    unlogged, same "no-op = no audit entry" convention as plan_task/
    validate_plan.
    """
    plan = state.get("plan") or []
    failed_index = next((i for i, st in enumerate(plan) if st.get("status") == "echoue"), None)
    replan_count = state.get("replan_count", 0) + 1
    if failed_index is None:
        return {"replan_count": replan_count}
    thread_id = config.get("configurable", {}).get("thread_id", "")

    first_human = next((m for m in state["messages"] if getattr(m, "type", None) == "human"), None)
    objective = first_human.content if first_human and isinstance(first_human.content, str) else ""
    done = "; ".join(st["description"] for st in plan[:failed_index] if st.get("status") == "fait")
    failure_reason = plan[failed_index].get("result") or "critère non atteint après plusieurs tentatives"
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
        f"Déjà accompli : {done or 'rien'}\n"
        f"Sous-tâche en échec : {plan[failed_index]['description']} — raison : {failure_reason}\n"
        f"{snapshot_hint}"
        "Replanifie le RESTE de la tâche à partir de maintenant, en tenant compte de cet échec et de ce qui existe réellement."
    )
    try:
        tools_hint = await _available_tools_hint()
        response = await planner_llm.ainvoke(
            [SystemMessage(content=PLANNER_SYSTEM_PROMPT), HumanMessage(content=context + tools_hint)]
        )
        new_subtasks = _validate_plan_json(response.content)
    except Exception:
        logger.warning("Replanning failed, retrying on the same subtask.", exc_info=True)
        new_plan = [dict(st) for st in plan]
        new_plan[failed_index]["status"] = "en_cours"
        new_plan[failed_index]["attempts"] = 0
        boundaries = (state.get("subtask_message_start") or [])[:failed_index]
        boundaries.append(len(state["messages"]))
        audit_log.log_message(
            thread_id, "replanning",
            {"replan_index": replan_count, "failed_subtask_index": failed_index, "new_subtask_count": None},
        )
        return {"plan": new_plan, "replan_count": replan_count, "subtask_message_start": boundaries}

    rebuilt = [dict(st) for st in plan[:failed_index]]
    for i, st in enumerate(new_subtasks):
        rebuilt.append({**st, "status": "en_cours" if i == 0 else "a_faire", "attempts": 0, "result": None})
    boundaries = (state.get("subtask_message_start") or [])[:failed_index]
    boundaries.append(len(state["messages"]))
    logger.info(
        "Replan #%d after subtask %d failure: %d new subtask(s)",
        replan_count, failed_index, len(new_subtasks),
    )
    audit_log.log_message(
        thread_id, "replanning",
        {"replan_index": replan_count, "failed_subtask_index": failed_index, "new_subtask_count": len(new_subtasks)},
    )
    return {"plan": rebuilt, "replan_count": replan_count, "subtask_message_start": boundaries}


async def report_failure(state: AgentState) -> dict:
    """
    Terminal (Iteration 2): reached when a subtask is "echoue" AND the
    replanning budget (REPLAN_BUDGET) is exhausted. HONEST report of the
    state reached — never a false success, never an infinite loop.
    """
    plan = state.get("plan") or []
    lines = ["Je n'ai pas pu terminer la tâche avec le budget de tentatives/replanifications disponible."]
    lines.append("État atteint :")
    for st in plan:
        label = _PLAN_STATUS_LABELS_GRAPH.get(st.get("status"), st.get("status", "?"))
        detail = f" — {st['result']}" if st.get("result") else ""
        lines.append(f"- [{label}] {st.get('description', '')}{detail}")
    return {"messages": [{"role": "assistant", "content": "\n".join(lines)}]}


def route_after_verification(state: AgentState) -> str:
    """
    Routing after verify_action (Iteration 2, wiring revised in Iteration
    4 — latency fix 1/2, then 1/2-bis, see docs/history.md). verify_action
    now runs AFTER call_llm (no longer BEFORE, see build_graph): this
    routing delegates directly to has_tool_calls (same 4 outcomes:
    auto_call_tools/call_tools/retry_empty_answer/end), state["messages"][-1]
    staying the same AIMessage throughout (verify_action never touches
    "messages").

    Fix 1/2-bis: the "subtask echoue -> replan/give_up" dispatch was MOVED
    to route_after_tool_execution (after the tool_calls execute, no
    longer here beforehand). Reason: the observation now lives in a
    mandatory tool call (report_and_act), which ALWAYS needs an
    acknowledgment ToolMessage to stay valid in the OpenAI format —
    jumping straight to replan_task/report_failure without executing this
    tool_calls (as before, when the observation lived in free text with
    no tool_calls to ever resolve) would leave an unresolved tool_call in
    the history, breaking the next LLM call that replays that history.
    """
    return has_tool_calls(state)


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

    # Traceability only (parity with auto_call_tools): never influences
    # execution — the sensitive tier has already been ruled out by
    # _route_slash_command_tier before reaching here.
    grants = state.get("session_grants") or []
    tier = approval_policy.effective_tier(tool_name, args, grants)
    thread_id = config.get("configurable", {}).get("thread_id", "")

    async with httpx.AsyncClient(timeout=60) as client:
        result, images = await _call_mcp_tool(client, tool_name, args, thread_id)

    if tier == approval_policy.TIER_REVERSIBLE:
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


def route_after_tool_execution(state: AgentState) -> str:
    """
    Routing after call_tools/auto_call_tools (latency fix 1/2-bis, see
    route_after_verification for why this moved here):

    1. Subtask "echoue" (verify_action just marked it, attempt budget
       exhausted, see above) -> replan_task/report_failure — the turn's
       tool_calls (at minimum report_and_act) was just executed right
       before, hence already resolved by a ToolMessage, whichever path is
       chosen next.
    2. Shortcut otherwise: if the turn's ONLY tool_calls was
       report_and_act (no real action decided) AND that same turn already
       carried a visible answer (frequent case: last subtask reached,
       final answer given in the same turn as its observation), looping
       back to call_llm would cost a whole LLM call just to have the
       model repeat an answer already produced — exactly the cost this
       effort aims to eliminate. Routes to finalize_after_report rather
       than straight to END (see that node: without it, the thread's last
       message would be report_and_act's acknowledgment ToolMessage, not
       the visible answer — breaking _current_answer/app/main.py, which
       assumes everywhere that messages[-1] is the answer's AIMessage).
    3. Normal case (a real action was also executed, or no visible
       answer): unchanged behavior, back to call_llm.
    """
    plan = state.get("plan") or []
    if any(st.get("status") == "echoue" for st in plan):
        if state.get("replan_count", 0) < REPLAN_BUDGET:
            return "replan"
        return "give_up"

    last_ai = next((m for m in reversed(state["messages"]) if getattr(m, "type", None) == "ai"), None)
    if last_ai is None:
        return "call_llm"
    tool_calls = getattr(last_ai, "tool_calls", None) or []
    only_report = bool(tool_calls) and all(tc["name"] == _REPORT_AND_ACT_TOOL_NAME for tc in tool_calls)
    if only_report and has_visible_answer(last_ai.content):
        return "finalize"
    return "call_llm"


async def finalize_after_report_and_act(state: AgentState) -> dict:
    """
    See route_after_tool_execution ("finalize"): re-emits the text of the
    answer already produced (and already streamed to the client by
    call_llm) as a NEW clean AIMessage, WITHOUT tool_calls — so that
    messages[-1] stays the visible answer's AIMessage, not the
    acknowledgment ToolMessage from report_and_act that was just
    executed. Same precedent as run_slash_command_direct (see its
    docstring): no LLM call, a plain standard-shaped message to stay
    compatible with app/main.py.
    """
    last_ai = next((m for m in reversed(state["messages"]) if getattr(m, "type", None) == "ai"), None)
    content = last_ai.content if last_ai is not None else ""
    return {"messages": [{"role": "assistant", "content": content}]}


def build_graph(checkpointer=None):
    graph = StateGraph(AgentState)
    graph.add_node("retrieve_context", retrieve_context)
    graph.add_node("select_skill", select_skill)
    graph.add_node("plan_task", plan_task)
    graph.add_node("validate_plan", validate_plan)
    graph.add_node("revise_plan", revise_plan)
    graph.add_node("require_plan_approval", require_plan_approval)
    graph.add_node("reject_plan", reject_plan)
    graph.add_node("call_llm", call_llm)
    graph.add_node("require_approval", require_approval)
    graph.add_node("call_tools", call_tools)
    graph.add_node("auto_call_tools", auto_call_tools)
    graph.add_node("verify_action", verify_action)
    graph.add_node("finalize_after_report_and_act", finalize_after_report_and_act)
    graph.add_node("replan_task", replan_task)
    graph.add_node("report_failure", report_failure)
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
    graph.add_edge("select_skill", "plan_task")
    graph.add_edge("plan_task", "validate_plan")
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
    # verify_action runs AFTER call_llm (analysis of the observation that
    # same call just produced, see docs/history.md "latency fix") —
    # route_after_verification delegates to has_tool_calls
    # (call_tools/auto_call_tools/retry_empty_answer/end). The
    # replan/give_up dispatch on a "echoue" subtask lives in
    # route_after_tool_execution, not here.
    graph.add_edge("call_llm", "verify_action")
    graph.add_conditional_edges(
        "verify_action",
        route_after_verification,
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
    graph.add_conditional_edges(
        "call_tools",
        route_after_tool_execution,
        {"call_llm": "call_llm", "finalize": "finalize_after_report_and_act", "replan": "replan_task", "give_up": "report_failure"},
    )
    graph.add_conditional_edges(
        "auto_call_tools",
        route_after_tool_execution,
        {"call_llm": "call_llm", "finalize": "finalize_after_report_and_act", "replan": "replan_task", "give_up": "report_failure"},
    )
    graph.add_edge("replan_task", "validate_plan")
    graph.add_edge("report_failure", END)
    graph.add_edge("finalize_after_report_and_act", END)
    # reject_tools also resolves ALL of the turn's tool_calls (including
    # report_and_act, see reject_tools) — same post-execution routing as
    # call_tools/auto_call_tools, so as never to skip over an
    # echoue/give_up potentially set by verify_action just before
    # (latency fix 1/2-bis: this case wasn't exercised by the tests
    # before report_and_act made a tool_calls near-systematic on verified
    # turns).
    graph.add_conditional_edges(
        "reject_tools",
        route_after_tool_execution,
        {"call_llm": "call_llm", "finalize": "finalize_after_report_and_act", "replan": "replan_task", "give_up": "report_failure"},
    )
    graph.add_edge("retry_empty_answer", "call_llm")

    return graph.compile(checkpointer=checkpointer or MemorySaver())


agent_graph = build_graph()
