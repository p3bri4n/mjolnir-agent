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
"read" or "reversible" tier by app/approval_policy.py (GhostDesk
mouse/screenshot, filesystem/git reads, by default — see that module for
the tier detail). The graph is therefore compiled with a checkpointer
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

# Format sent to the LLM for tool image results (GhostDesk screen_shot,
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
# reaching the auto-approved GhostDesk loop (capture/click) which alone
# consumes 2 iterations per gesture. Overflow reported explicitly to the
# user rather than silently (see _current_answer, app/main.py).
MAX_TOOL_ITERATIONS = int(os.environ.get("MAX_TOOL_ITERATIONS", "20"))

# Approval policy by reversibility tier (see app/approval_policy.py): a
# turn is auto-approved if ALL its tool_calls are "read" or "reversible"
# tier; a mixed turn (even a single "sensitive"-tier tool) stays fully
# subject to approval, for safety — no partial per-tool approval.
# AUTO_APPROVED_TOOLS (old env var) keeps working as a backward-compatible
# override, handled in approval_policy.tool_tier().

# Number of consecutive auto-approved turns tolerated before forcing a
# pass through require_approval anyway, even if all the turn's tool_calls
# remain auto-approved ("read"/"reversible" tier) — the guardrail against
# a virtual keyboard: a single click is harmless, but a SEQUENCE of clicks
# could compose full text input via an on-screen virtual keyboard,
# effectively bypassing the key_type/key_press ("sensitive" tier)
# exclusion. Without a cap, a long click sequence could ultimately type
# any text without a human ever validating anything. Reset to 0 on every
# real pass through require_approval (see this function below), not just
# at the start of a new task — unlike tool_iterations, which measures a
# total budget rather than a number of consecutive turns WITHOUT human
# supervision.
AUTO_APPROVAL_STREAK_LIMIT = int(os.environ.get("AUTO_APPROVAL_STREAK_LIMIT", "6"))

# Image retention in the history submitted to the LLM: every GhostDesk
# screenshot (screen_shot) adds a multimodal message costly in visual
# tokens (see _split_image_blocks); on a repeated capture/click loop,
# keeping ALL of them ends up saturating the context for near-zero value
# (only the most recent capture reflects the screen's current state).
# Keeps only the last MAX_IMAGES_IN_CONTEXT images in what's sent to the
# LLM; earlier ones are replaced by a placeholder text — only for THIS
# call (see _apply_image_retention), never persisted in the graph's
# state/checkpointer: the full history (with all original images) stays
# unchanged and replayable/inspectable.
MAX_IMAGES_IN_CONTEXT = int(os.environ.get("MAX_IMAGES_IN_CONTEXT", "1"))
IMAGE_RETENTION_PLACEHOLDER = "[screenshot antérieure supprimée]"

# Planner node (Iteration 1, Phase 1 "cognitive core" — see
# docs/briefs/phase-1-coeur-cognitif.md). DEFAULT FLIPPED (docs/briefs/
# flags-du-coeur-cognitif.md): the "false" default dated back to
# iteration-by-iteration validation, where an extra LLM call at the start
# of EVERY task would have broken almost all existing tests, which
# mocked a FIXED sequence of replies. The cognitive core is now measured
# (final campaign 29/33, consistent with pre-cognitive-core Campaign A at
# 30/33 — see docs/history.md/README) and adopted: the NOMINAL behavior
# must be the default, it's DISABLING it that must be explicit. Tests
# that depend on pre-cognitive-core behavior now explicitly force
# "false", they no longer rely on the default.
PLANNER_ENABLED = os.environ.get("PLANNER_ENABLED", "true").lower() == "true"

# Post-action verification + failure budget (Iteration 2, Phase 1
# "cognitive core" — see docs/briefs/phase-1-coeur-cognitif.md). ONLY HAS
# AN EFFECT IF PLANNER_ENABLED IS ALSO ON: verification compares a
# tool-call turn's result to the ACTIVE subtask's success_criterion (see
# verify_action below) — nothing to verify without a plan. DEFAULT
# FLIPPED, same justification as PLANNER_ENABLED above (measured and
# adopted, see docs/briefs/flags-du-coeur-cognitif.md).
VERIFICATION_ENABLED = os.environ.get("VERIFICATION_ENABLED", "true").lower() == "true"
# Attempts per subtask before marking it "echoue" (see verify_action).
SUBTASK_ATTEMPT_BUDGET = int(os.environ.get("SUBTASK_ATTEMPT_BUDGET", "3"))
# Replans tolerated for a single task before honestly giving up (see
# replan_task/report_failure) rather than looping forever or claiming a
# false success.
REPLAN_BUDGET = int(os.environ.get("REPLAN_BUDGET", "2"))

# Plan validation pipeline (Iteration 3, Phase 1 "cognitive core" — see
# docs/briefs/phase-1-coeur-cognitif.md and app/plan_validation.py). ONLY
# HAS AN EFFECT IF PLANNER_ENABLED IS ALSO ON. DEFAULT FLIPPED, same
# justification as PLANNER_ENABLED/VERIFICATION_ENABLED above (see
# docs/briefs/flags-du-coeur-cognitif.md).
PLAN_VALIDATION_ENABLED = os.environ.get("PLAN_VALIDATION_ENABLED", "true").lower() == "true"
# LLM judge of the plan (heuristics already passed, costly — one LLM call
# per validation). WITHDRAWAL CLAUSE (Iteration 3 brief) measured under
# real conditions (see docs/history.md, Iteration 3): it did really veto a
# plan the heuristics let through, for semantic reasons beyond their
# reach (proof of real usefulness, not a "theater" validator), at the
# cost of noticeable latency. DEFAULT FLIPPED (docs/briefs/
# flags-du-coeur-cognitif.md): explicit decision to enable it by default
# along with the 3 flags above, the final campaign at 29/33 having
# measured all 4 flags active together.
PLAN_JUDGE_ENABLED = os.environ.get("PLAN_JUDGE_ENABLED", "true").lower() == "true"
# "Justified rejection → back to the planner, max 2 cycles then human
# escalation" (brief): number of rejections (heuristics OR judge)
# tolerated before a human decides (require_plan_approval, with the
# rejection reasons displayed) rather than letting the planner loop
# indefinitely.
PLAN_VALIDATION_CYCLES_MAX = 2

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

# The served VLM (Qwen3.6 MoE) reasons well but localizes poorly: its
# visual grounding (aiming at the right on-screen pixel for an element)
# remains imprecise, with no dedicated OCR/UI-element detection (see
# README, Known, accepted limitations). find_text/read_screen
# (services/ocr-service, read tier — see approval_policy.py) compensate
# with exact OCR coordinates. Transient instruction (never persisted in
# the graph's state, same principle as NO_THINK_DIRECTIVE above) rather
# than a per-turn system prompt change: stays identically valid across
# the whole conversation. Kept in French: sent to the model, behavior not
# documentation (CLAUDE.md rule #11).
GROUNDING_DIRECTIVE = (
    "Pour cliquer sur un élément contenant du texte, appelle d'abord "
    "find_text pour obtenir ses coordonnées exactes plutôt que d'estimer "
    "visuellement leur position — réserve l'estimation visuelle aux "
    "éléments sans texte (icônes)."
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
    system block (after GROUNDING_DIRECTIVE/DOWNLOAD_DIRECTIVE/
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
    Capture un browser_snapshot FRAIS au moment de la vérification —
    correctif d'ancrage trouvé pendant la sonde live de l'Itération 4 (voir
    docs/history.md) : le résultat brut du dernier tool_call (ex. la
    confirmation d'un browser_click) est souvent TERSE, sans le contenu de
    la page qui en résulte. verify_action jugeait alors une sous-tâche
    "échouée" en se fiant uniquement à success_criterion — parfois lui-même
    mal ancré (ex. "utilise la barre de recherche" sur un site qui n'en a
    pas) — sans jamais voir que la page réelle montrait déjà une
    progression valide (ex. pagination). Best-effort : erreur mcp-client ->
    chaîne vide, le vérificateur juge alors avec les seules infos déjà
    disponibles (comportement identique à avant ce correctif) — jamais un
    blocage pour un souci de capture annexe, même philosophie que le reste
    de ce fichier.
    """
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            result, _ = await _call_mcp_tool(client, "browser_snapshot", {})
        truncated = _truncate_browser_result(result, BROWSER_TOOL_OUTPUT_MAX_CHARS, objective)
        blocks = truncated.get("content", [])
        texts = [b["text"] for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(texts)
    except Exception:
        logger.warning("Capture de vérification (browser_snapshot) indisponible, jugement sans elle.", exc_info=True)
        return ""


async def _grounding_snapshot(state: dict, objective: str) -> Optional[str]:
    """
    Snapshot de la page courante pour ancrer une (re)planification/
    validation sur ce qui existe RÉELLEMENT (Itération 4, suite du
    correctif verify_action — voir docs/history.md). `None` si aucune navigation
    n'a encore eu lieu pour cette tâche (state["current_page_url"], Phase
    1) : le TOUT PREMIER plan (plan_task) reste donc structurellement non
    ancré — aucune page n'existe encore à capturer à ce stade, et forcer
    une navigation exploratoire avant la planification soulèverait ses
    propres questions de tier/approbation (browser_navigate est
    TIER_SENSITIVE), hors périmètre ici. Les REPLANIFICATIONS
    (revise_plan/replan_task), elles, sont toujours déclenchées APRÈS
    qu'une navigation a eu lieu — c'est là que ce correctif s'applique.
    """
    if not state.get("current_page_url"):
        return None
    return await _fetch_verification_snapshot(objective) or None


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    tool_iterations: int
    approved: Optional[bool]
    # Tours auto-approuvés consécutifs depuis le dernier passage par
    # require_approval (voir AUTO_APPROVAL_STREAK_LIMIT plus haut).
    auto_approval_streak: int
    # Nombre de messages Open WebUI (rôles user/assistant) déjà intégrés à ce
    # thread — permet à app/main.py de ne soumettre que les nouveaux messages
    # à chaque tour plutôt que tout l'historique renvoyé par Open WebUI (qui
    # est déjà persisté ici via le checkpointer), et donc d'éviter de le
    # dupliquer dans "messages" à chaque tour.
    owui_message_count: int
    # État de la balise <think> (voir _think_state plus haut), reporté d'un
    # appel de call_llm à l'autre au sein d'un même tour utilisateur — requis
    # depuis AUTO_APPROVED_TOOLS, qui permet à call_llm de s'exécuter plusieurs
    # fois de suite sans pause d'approbation entre deux. Sans ce report, chaque
    # itération rouvrait sa propre balise <think>, et Open WebUI n'affiche en
    # bulle repliable que celle en tout début de message : les suivantes
    # apparaissaient en texte brut visible. Remis à False à chaque nouveau tour
    # (voir _resolve_run, app/main.py), comme tool_iterations.
    think_opened: bool
    think_closed: bool
    # Grants de session (Phase 3) : noms d'outils qu'un humain a approuvés
    # "pour la session" via require_approval (voir ce nœud plus bas) plutôt
    # qu'une fois seulement. Un outil dans cette liste est plafonné à
    # TIER_REVERSIBLE (auto + audit) pour le reste du thread, même s'il
    # serait normalement TIER_SENSITIVE (voir approval_policy.effective_tier).
    # Vit dans l'état du graphe, donc dans le checkpointer MemorySaver (en
    # mémoire uniquement) : un redémarrage du service perd les grants en même
    # temps que le reste du thread — comportement voulu, pas un bug (voir
    # README, section Supervision humaine).
    session_grants: list
    # Décision transitoire couplée à "approved" (voir require_approval) :
    # True si l'humain a répondu "approuver pour la session" plutôt que
    # "approuver" seul. Consommée puis remise à False dès que require_approval
    # a appliqué le grant, pour ne pas re-déclencher un grant à chaque reprise
    # ultérieure du thread.
    grant_session: bool
    # Compteur de retries pour le filet de sécurité "réponse vide" (voir
    # MAX_EMPTY_ANSWER_RETRIES plus haut) — budget cumulé pour toute la
    # tâche, comme tool_iterations, jamais remis à zéro entre deux retries.
    empty_answer_retries: int
    # Signal explicite (pas déduit de la forme des messages, trop fragile —
    # un tour LLM normal qui a analysé une image via vision produit aussi un
    # AIMessage juste après un message image) : True uniquement quand le
    # dernier message vient de run_slash_command_direct ET portait une
    # image, pour que main.py sache reconstruire l'affichage de l'image pour
    # CE tour (_render_visible_answer) sans la persister en base64 dans le
    # message assistant lui-même. call_llm le remet à False à chaque appel :
    # c'est le seul autre nœud qui termine un tour sur un AIMessage visible,
    # donc la seule remise à zéro nécessaire pour que ce signal reste correct
    # quelle que soit la façon dont ce tour se termine.
    slash_command_image_shown: bool
    # Garde-fou fabrication d'URL (Phase 1, voir _check_navigate_url) :
    # ensemble des URL "vues" pour cette tâche — cible de départ (racines du
    # périmètre, extraites du 1er message humain), navigations déjà
    # exécutées, et liens observés dans le contenu renvoyé par un outil
    # browser_* (snapshot/DOM). Remis à zéro à chaque nouveau tour utilisateur
    # (voir run_input, app/main.py), comme tool_iterations — le périmètre est
    # celui de LA TÂCHE en cours, pas de toute la conversation.
    observed_urls: list
    # URL de la page actuellement chargée dans le navigateur (dernière valeur
    # "Page URL: ..." vue dans un résultat d'outil browser_*), nécessaire pour
    # résoudre les liens RELATIFS (ex. "/catalog/product-14.html") en URL
    # absolues avant de les ajouter à observed_urls.
    current_page_url: Optional[str]
    # Liens de la DERNIÈRE page vue (remplacés, pas accumulés, contrairement
    # à observed_urls) : utilisés pour orienter le modèle vers de vrais
    # liens quand une navigation fabriquée est refusée (voir
    # _execute_tool_calls) — "voici où tu es réellement", pas tout
    # l'historique de navigation qui serait moins actionnable.
    current_page_links: list
    # Compteur de tentatives de navigation vers une URL non observée,
    # bloquées AVANT exécution (voir _check_navigate_url) — métrique Phase 1,
    # pas juste un frein silencieux.
    fabricated_navigation_attempts: int
    # Plan explicite de la tâche (Itération 1, Phase 1 « cœur cognitif » —
    # voir docs/briefs/phase-1-coeur-cognitif.md et plan_task plus bas) :
    # liste de {description, success_criterion, status, attempts, result}.
    # status ∈ {"a_faire", "en_cours", "fait", "echoue"} (string libre, pas
    # d'enum dédié — cohérent avec failure_cause dans le harnais de tests).
    # Calculé UNE FOIS par plan_task au tout début d'une tâche (liste vide ->
    # le planificateur tourne ; non vide -> passthrough, jamais reconstruit
    # au sein d'une même tâche). Remis à [] à chaque NOUVEAU message
    # utilisateur top-level (voir run_input, app/main.py), comme
    # observed_urls. Aucune validation/tier/vérification post-action
    # branchée dessus pour l'instant (Itérations 2/3 à venir) : structure et
    # visibilité seules à l'Itération 1 ; vérification post-action/budget
    # d'échec branchés dessus depuis l'Itération 2 (voir verify_action,
    # replan_task, report_failure plus bas). No-op tant que PLANNER_ENABLED
    # est désactivé (défaut) : reste alors toujours [].
    plan: list
    # Nombre de replanifications déjà effectuées pour CETTE tâche (Itération
    # 2, voir replan_task/route_after_verification) — budget cumulé, comme
    # tool_iterations, plafonné par REPLAN_BUDGET. Remis à 0 à chaque
    # nouveau message utilisateur top-level (voir run_input, app/main.py).
    replan_count: int
    # Pipeline de validation du plan (Itération 3, voir validate_plan/
    # revise_plan/require_plan_approval plus bas). plan_validation_reasons :
    # motifs du DERNIER rejet (heuristiques et/ou juge), [] si le plan
    # courant est valide (ou pas encore évalué). plan_validation_cycles :
    # nombre de rejets subis pour CETTE tâche (pas par plan proposé — un
    # budget partagé entre planification initiale et replanifications,
    # voir PLAN_VALIDATION_CYCLES_MAX), au-delà escalade humaine plutôt que
    # de reboucler indéfiniment sur le planificateur. Les deux remis à
    # zéro/vide à chaque nouveau message utilisateur top-level (voir
    # run_input, app/main.py).
    plan_validation_reasons: list
    plan_validation_cycles: int
    # Approbation du plan (Itération 3) : miroir de approved/grant_session
    # (require_approval) mais pour le PLAN entier plutôt qu'un tool_call —
    # voir require_plan_approval. plan_grant : persisté (contrairement à
    # plan_grant_session, transitoire) — un plan-level grant accordé une
    # fois évite la pause sur une replanification ultérieure DANS LA MÊME
    # TÂCHE tant que le nouveau tier reste TIER_REVERSIBLE ou moins, jamais
    # pour TIER_SENSITIVE (même philosophie que NEVER_GRANTABLE_TOOLS,
    # approval_policy.py).
    plan_approved: Optional[bool]
    plan_grant_session: bool
    plan_grant: bool
    # True dès qu'une action vient d'être exécutée (_execute_tool_calls),
    # consommé par verify_action au tour suivant — plus robuste qu'une
    # recherche du dernier tool_call dans l'historique : sans ce marqueur
    # explicite, un tour de replanification (qui n'exécute AUCUN outil)
    # pourrait être confondu avec une action encore à constater si le tour
    # précédent était resté sans tool_calls.
    pending_verification: bool
    # Compteur cumulatif (remis à 0 à chaque nouveau message utilisateur
    # top-level, run_input/app/main.py), incrémenté quand
    # pending_verification était vrai mais qu'aucun constat exploitable n'a
    # pu être extrait du tour. Dégradation VOLONTAIREMENT inversée : ce cas
    # se MESURE (métrique dédiée) plutôt que de se FACTURER comme un échec
    # de sous-tâche (voir verify_action et docs/history.md, "correctif latence",
    # pour le score cassé par l'ancien mécanisme qui le comptait comme un
    # échec).
    constats_inexploitables: int


# Plafond de tokens par TOUR (un seul appel LLM), pas pour la conversation
# entière : sans lui, une dérive en boucle de répétition (observée en usage
# réel avec un modèle très quantisé — voir README) génère jusqu'à saturer
# tout le contexte avant de s'arrêter (des dizaines de secondes, des milliers
# de tokens), sans jamais produire de tool_calls ni déclencher nos propres
# garde-fous (MAX_TOOL_ITERATIONS/AUTO_APPROVAL_STREAK_LIMIT), qui ne comptent
# que des itérations d'outils, pas la longueur d'une génération.
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "2048"))

llm = ChatOpenAI(
    base_url=LLM_BASE_URL,
    api_key="not-needed",       # tabbyapi (disable_auth: true)/llama-server/Ollama ne vérifient pas la clé par défaut
    model="agent-llm",          # doit matcher model_name dans services/tabbyapi/config.yml
    temperature=0.2,
    max_tokens=LLM_MAX_TOKENS,
)

# Bug découvert en conditions réelles en vérifiant la campagne live de
# l'Itération 3 (voir docs/history.md) : les appels LLM auxiliaires (plan_task/
# revise_plan/verify_action/_judge_plan) utilisaient `llm` ci-dessus, plafonné
# à LLM_MAX_TOKENS (2048, pensé pour le tour conversationnel principal).
# Qwen3.6/TabbyAPI raisonne dans un champ reasoning_content SÉPARÉ de
# content avant de répondre (confirmé par un appel direct à TabbyAPI hors
# streaming) ; ce raisonnement, souvent long, consommait à lui seul tout le
# budget, tronquant `content` à vide ou au milieu du JSON
# (finish_reason="length") — chaque validateur retombait alors
# systématiquement sur son repli d'erreur, jamais sur une vraie évaluation.
# `/no_think` en préfixe de prompt (mécanisme ADAPTIVE_THINKING existant)
# ne supprime PAS le raisonnement sur ce backend (vérifié par le même appel
# direct) — solution retenue : un budget de tokens plus généreux, dédié à
# ces appels structurés, séparé du budget de la boucle principale (dont la
# petite valeur reste un filet de sécurité voulu contre les dérives de
# répétition, voir LLM_MAX_TOKENS).
PLANNER_MAX_TOKENS = int(os.environ.get("PLANNER_MAX_TOKENS", "8192"))
# Thinking bridé sur les appels auxiliaires (plan_task/revise_plan/
# replan_task/_judge_plan, tous via planner_llm) — contrairement à
# `/no_think` en préfixe de prompt (ADAPTIVE_THINKING, confirmé sans effet
# sur ce backend, voir commentaire plus haut), TabbyAPI expose un vrai
# paramètre PAR REQUÊTE côté serveur
# (`GET /openapi.json`, schéma ChatCompletionRequest : `enable_thinking:
# bool`), vérifié EN DIRECT avant d'écrire ce correctif (appel réel avec un
# prompt de planification JSON, voir docs/history.md) : `reasoning_content:
# null`, JSON valide immédiat, aucun raisonnement. `extra_body` est un
# paramètre natif de langchain-openai (vérifié :
# `"extra_body" in inspect.signature(ChatOpenAI).parameters`).
# PLANNER_THINKING_ENABLED (défaut false = thinking bridé) plutôt qu'une
# désactivation en dur : permet un rollback sans redéploiement de code si
# la qualité des plans/jugements s'en trouvait dégradée en pratique.
PLANNER_THINKING_ENABLED = os.environ.get("PLANNER_THINKING_ENABLED", "false").lower() == "true"
planner_llm = ChatOpenAI(
    base_url=LLM_BASE_URL,
    api_key="not-needed",
    model="agent-llm",
    temperature=0.2,
    max_tokens=PLANNER_MAX_TOKENS,
    extra_body={"enable_thinking": PLANNER_THINKING_ENABLED},
)

# Schéma des outils MCP (terminal/filesystem/git/browser/desktop-GhostDesk),
# récupéré depuis mcp-client et mis en cache pour la durée du process. Sans
# ce bind_tools, le LLM n'a aucune connaissance de l'existence de ces outils
# et ne peut donc jamais produire de tool_calls, quel que soit le modèle
# servi — has_tool_calls()/require_approval() restent alors du code mort.
_tools_schema_cache: Optional[list] = None


async def _get_tools_schema() -> list:
    """Remplit/retourne _tools_schema_cache — factorisé hors de _get_bound_llm
    pour être aussi utilisable par _route_entry (validation du nom d'outil
    d'une commande slash) sans requête HTTP supplémentaire une fois en cache."""
    global _tools_schema_cache
    if _tools_schema_cache is None:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{MCP_CLIENT_URL}/tools/schema")
                resp.raise_for_status()
                _tools_schema_cache = resp.json().get("tools", [])
        except (httpx.HTTPError, ValueError):
            # mcp-client injoignable ou réponse invalide : dégrade sans outils
            # plutôt que de faire échouer toute la conversation.
            _tools_schema_cache = []
    return _tools_schema_cache


async def _get_bound_llm() -> ChatOpenAI:
    schema = await _get_tools_schema()
    if not schema:
        return llm
    if not VERIFICATION_ENABLED:
        return llm.bind_tools(schema)
    # constat_precedent injecté comme paramètre requis de CHAQUE outil MCP
    # réel, plus report_and_act comme seul repli (tour en texte pur, aucune
    # action) — voir plus haut. Gated sur
    # VERIFICATION_ENABLED : sans lui, ce champ n'a aucun lecteur
    # (_verification_directive ne l'instruit pas) et ne ferait qu'ajouter
    # du bruit au schéma envoyé au modèle.
    wrapped = [_inject_constat_param(t) for t in schema]
    return llm.bind_tools(wrapped + [_REPORT_AND_ACT_TOOL])


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
    Liste réelle des outils MCP disponibles (découvert en conditions
    réelles pendant la campagne live de l'Itération 3, voir docs/history.md) :
    sans elle, le planificateur invente des noms d'outils plausibles mais
    inexistants (ex. "web_browser", "search") — systématiquement rejetés
    par les heuristiques (outils référencés existants,
    app/plan_validation.py), aucun plan ne passerait jamais la validation.
    Ajoutée au message UTILISATEUR (pas au system prompt, figé lui) pour
    rester à jour si le schéma d'outils change entre deux tâches. Utilisée
    par plan_task/revise_plan/replan_task.
    """
    schema = await _get_tools_schema()
    names = sorted({t.get("function", {}).get("name") for t in schema} - {None})
    if not names:
        return ""
    return (
        "\n\nOutils réellement disponibles (utilise UNIQUEMENT ces noms exacts "
        'dans "outils", liste vide si aucun ne s\'applique) : ' + ", ".join(names)
    )


async def plan_task(state: AgentState) -> dict:
    """
    Nœud planificateur (Itération 1, Phase 1 « cœur cognitif »). No-op
    (`{"messages": []}`) si PLANNER_ENABLED est désactivé (défaut), si un
    plan existe déjà pour cette tâche (calculé une seule fois, jamais
    reconstruit au sein d'une même tâche — voir AgentState.plan) ou s'il n'y
    a aucun message humain à planifier.

    Appel LLM séparé de call_llm : `llm` brut (jamais `bound_llm`), le
    planificateur ne doit jamais émettre de tool_calls, seulement du JSON.

    Dégrade TOUJOURS sur un plan à sous-tâche unique plutôt que de bloquer
    la tâche pour un souci de planification annexe (transport HTTP, réponse
    invalide) — capture large volontaire (PlanValidationError ou n'importe
    quelle erreur du client OpenAI/httpx), même esprit que la dégradation
    httpx.HTTPError de retrieve_context/select_skill ci-dessus, élargie ici
    car l'échec peut aussi venir de la validation JSON, pas seulement du
    transport.
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
        logger.warning("Planification échouée, repli sur un plan à sous-tâche unique.", exc_info=True)
        subtasks = [{"description": objective, "success_criterion": "objectif de la tâche atteint", "tools": []}]

    plan = [{**st, "status": "a_faire", "attempts": 0, "result": None} for st in subtasks]
    if plan:
        plan[0]["status"] = "en_cours"
    logger.info("Plan initial (%d sous-tâche(s)) : %s", len(plan), plan)
    return {"plan": plan}


def _plan_tier(plan: list) -> str:
    """
    Tier du plan = pire tier parmi TOUS les outils déclarés par ses
    sous-tâches (Itération 3) — approval_policy.tool_tier(), qui retombe
    déjà sur TIER_SENSITIVE pour un outil inconnu (défaut existant "outil
    inconnu = toujours sensible", cohérent ici). Aucun outil déclaré nulle
    part -> TIER_READ (rien à approuver en amont).
    """
    tiers = {approval_policy.tool_tier(tool) for subtask in plan for tool in subtask.get("tools", [])}
    if approval_policy.TIER_SENSITIVE in tiers:
        return approval_policy.TIER_SENSITIVE
    if approval_policy.TIER_REVERSIBLE in tiers:
        return approval_policy.TIER_REVERSIBLE
    return approval_policy.TIER_READ


async def validate_plan(state: AgentState) -> dict:
    """
    Pipeline de validation du plan (Itération 3, Phase 1 « cœur cognitif »).
    No-op (`{"messages": []}`) si PLAN_VALIDATION_ENABLED désactivé
    (défaut) ou si `state["plan"]` est vide — comportement identique à
    avant cette itération. Sinon : heuristiques programmatiques
    (app/plan_validation.py, gratuites) puis, UNIQUEMENT si elles passent
    ET que PLAN_JUDGE_ENABLED, juge LLM (coûteux — clause de retrait, voir
    docs/history.md). Rejet (heuristiques OU juge) -> plan_validation_cycles
    incrémenté, motifs renvoyés pour route_after_validation.
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
    reasons = plan_validation.validate_plan_heuristics(plan, known_tools=known_tools, task_scope_urls=task_scope)

    if not reasons and PLAN_JUDGE_ENABLED:
        first_human = next((m for m in state["messages"] if getattr(m, "type", None) == "human"), None)
        objective = first_human.content if first_human and isinstance(first_human.content, str) else ""
        page_snapshot = await _grounding_snapshot(state, objective)
        reasons = await _judge_plan(plan, objective, page_snapshot)

    if reasons:
        cycles = state.get("plan_validation_cycles", 0) + 1
        logger.warning("Plan rejeté par la validation (cycle %d) : %s", cycles, reasons)
        # plan_approved réarmé à None ICI (pas dans require_plan_approval,
        # voir son commentaire) : que ce rejet mène à une révision ou à une
        # escalade humaine, toute décision précédente sur un plan ANTÉRIEUR
        # ne doit jamais être réutilisée pour celui-ci.
        return {"plan_validation_reasons": reasons, "plan_validation_cycles": cycles, "plan_approved": None}

    logger.info("Plan validé (%d sous-tâche(s)).", len(plan))
    return {"plan_validation_reasons": [], "plan_approved": None}


def route_after_validation(state: AgentState) -> str:
    """
    Routage après validate_plan. PLAN_VALIDATION_ENABLED désactivé ->
    "call_llm" (flux identique à avant cette itération). Rejeté ->
    "revise_plan" tant que PLAN_VALIDATION_CYCLES_MAX n'est pas dépassé,
    sinon "require_plan_approval" (escalade humaine, motifs affichés).
    Accepté -> "call_llm" si TIER_READ ou si TIER_REVERSIBLE et un grant de
    plan est déjà accordé pour cette tâche (plan_grant, jamais pour
    TIER_SENSITIVE), sinon "require_plan_approval" (approbation normale).
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
    Révision du plan suite à un rejet du pipeline de validation (Itération
    3). Distinct de replan_task (Itération 2, déclenché par un ÉCHEC
    D'EXÉCUTION d'une sous-tâche) : ici, rien n'a encore été exécuté — le
    plan lui-même est jugé structurellement/sémantiquement insuffisant
    AVANT le premier tour. Régénère le plan ENTIER (aucune sous-tâche
    "fait" à préserver) avec les motifs de rejet en contexte. Même repli
    que plan_task sur échec de génération (plan à sous-tâche unique).
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
        logger.warning("Révision du plan échouée, repli sur un plan à sous-tâche unique.", exc_info=True)
        subtasks = [{"description": objective, "success_criterion": "objectif de la tâche atteint", "tools": []}]

    plan = [{**st, "status": "a_faire", "attempts": 0, "result": None} for st in subtasks]
    if plan:
        plan[0]["status"] = "en_cours"
    logger.info("Plan révisé (%d sous-tâche(s), cycle de validation) : %s", len(plan), plan)
    return {"plan": plan}


async def require_plan_approval(state: AgentState) -> dict:
    """
    Approbation humaine du PLAN (Itération 3) : miroir de require_approval
    mais pour le plan entier plutôt qu'un tool_call — pause (NodeInterrupt)
    tant que plan_approved est None. Reste NON FUSIONNABLE avec
    l'approbation individuelle d'un outil TIER_SENSITIVE à l'exécution :
    ce nœud est un gate ADDITIONNEL en amont, require_approval/
    _execute_tool_calls restent inchangés et s'appliquent quand même.
    """
    if state.get("plan_approved") is None:
        raise NodeInterrupt("Approbation humaine du plan requise avant exécution.")
    # NE PAS remettre plan_approved à None ici : route_after_plan_approval
    # (juste après) doit encore pouvoir lire la décision (True/False) telle
    # que ce nœud vient de la recevoir — même piège déjà évité par
    # require_approval, qui laisse "approved" intact pour route_after_approval
    # et ne le réarme qu'ailleurs (_execute_tool_calls, pour le tour
    # suivant). Ici, c'est validate_plan qui réarme plan_approved à None à
    # chaque nouveau plan proposé (voir ce nœud).
    updates = {"plan_grant_session": False}
    if state.get("plan_grant_session"):
        updates["plan_grant"] = True
    return updates


def route_after_plan_approval(state: AgentState) -> str:
    return "call_llm" if state["plan_approved"] else "reject_plan"


async def reject_plan(state: AgentState) -> dict:
    """Miroir de reject_tools, côté plan : l'humain a refusé le plan proposé, la tâche s'arrête ici."""
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
    Décomposition approximative (voir estimate_tokens) du contexte tel qu'il
    serait construit pour un appel LLM (voir call_llm), à l'usage de POST
    /context (app/main.py) et donc du dashboard d'observabilité
    (services/dashboard) — jamais un vrai appel au LLM, et le schéma d'outils
    est lu tel quel depuis _tools_schema_cache (jamais recalculé via
    _get_bound_llm, qui ferait un appel HTTP à mcp-client : /context doit
    rester strictement lecture seule, sans effet de bord, comme /pending).

    `messages` vide (thread inconnu du checkpointer) -> tous les blocs à
    zéro plutôt que d'inclure quand même le system prompt transitoire
    (GROUNDING_DIRECTIVE) : rien n'a encore été composé pour ce thread.
    """
    if not messages:
        return [
            {"label": label, "kind": kind, "est_tokens": 0, "count": 0}
            for label, kind in _CONTEXT_BLOCK_SKELETON
        ]

    system_parts = [GROUNDING_DIRECTIVE, DOWNLOAD_DIRECTIVE, BULK_CHECK_DIRECTIVE, PEREMPTION_DIRECTIVE]
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
    Ne garde que les MAX_IMAGES_IN_CONTEXT derniers messages image (voir
    _is_image_message) dans la liste envoyée au LLM ; les précédents sont
    remplacés par un message texte indicatif. Retourne une NOUVELLE liste
    (jamais de mutation en place des messages d'origine, qui sont les mêmes
    objets Python que ceux persistés par le checkpointer) — c'est ce qui
    garantit que ce filtrage reste local à cet appel, sans jamais toucher à
    l'état du graphe.
    """
    image_indices = [i for i, m in enumerate(messages) if _is_image_message(m)]
    cutoff = len(image_indices) - max(MAX_IMAGES_IN_CONTEXT, 0)
    if cutoff <= 0:
        return messages

    filtered = list(messages)
    for i in image_indices[:cutoff]:
        filtered[i] = HumanMessage(content=IMAGE_RETENTION_PLACEHOLDER)
    return filtered


def _previous_turn_tool_calls(messages: list) -> Optional[list]:
    """Dernier message AI avec tool_calls dans l'historique — le tour qui a mené à cet appel de call_llm."""
    for message in reversed(messages):
        if getattr(message, "type", None) == "ai" and getattr(message, "tool_calls", None):
            return message.tool_calls
    return None


def _apply_adaptive_thinking(messages: list, session_grants) -> list:
    """
    Ajoute un system prompt transitoire "/no_think" (jamais persisté dans
    l'état du graphe, voir _apply_image_retention pour le même principe)
    quand ADAPTIVE_THINKING est activé ET que le tour précédent était
    entièrement auto-approuvé (même politique par tiers que has_tool_calls) —
    typiquement une boucle perception-action GhostDesk (capture -> clic ->
    capture) où le raisonnement étendu de Qwen3.6 coûte plus qu'il n'apporte.
    Pas d'injection sur le tout premier tour d'une tâche (aucun tool_calls
    précédent) ni dès qu'un outil sensible était en jeu : le raisonnement y
    a le plus de valeur.
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
    # Fusionné dans le message système de tête s'il y en a un (cas réel :
    # GROUNDING_DIRECTIVE, ajouté par call_llm juste avant cet appel), sinon
    # ajouté en position 0 — jamais en fin de liste : certains backends
    # (TabbyAPI/ExLlamaV3, template Jinja strict de Qwen3.6) rejettent
    # explicitement un second message système ou un message système non en
    # tête ("TemplateError: System message must be at the beginning") —
    # llama-server/Ollama tolèrent les deux formes, donc ce bug restait
    # invisible avant la migration vers TabbyAPI.
    if messages and isinstance(messages[0], SystemMessage):
        head, *rest = messages
        merged_head = SystemMessage(content=f"{head.content}\n{NO_THINK_DIRECTIVE}")
        return [merged_head] + rest
    return [SystemMessage(content=NO_THINK_DIRECTIVE)] + messages


def _verification_directive(state: AgentState) -> str:
    """
    Injecte le constat sur l'action précédente dans le raisonnement du
    tour courant plutôt qu'un appel LLM séparé (historique dans docs/history.md,
    "correctif latence") — coût marginal ~zéro. Le rappel de base
    (constat_precedent requis sur CHAQUE tool_call, _inject_constat_param
    dans _get_bound_llm) est TOUJOURS injecté dès que VERIFICATION_ENABLED
    est actif, dès le tout premier outil appelé de la tâche (rien à
    constater alors -> "sans_objet"). Le hint SPÉCIFIQUE (critère de la
    sous-tâche active) reste conditionné à `pending_verification` +
    sous-tâche "en_cours" (DOIT rester synchronisé avec verify_action) :
    rien de neuf à constater sinon (ex. tour de replanification, qui
    n'exécute aucun outil).
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


async def call_llm(state: AgentState, config: dict) -> dict:
    bound_llm = await _get_bound_llm()
    messages_for_llm = [
        SystemMessage(
            content=(
                f"{GROUNDING_DIRECTIVE}\n{DOWNLOAD_DIRECTIVE}{BULK_CHECK_DIRECTIVE}{PEREMPTION_DIRECTIVE}"
                f"{_date_directive()}{_verification_directive(state)}"
            )
        )
    ] + state["messages"]
    messages_for_llm = _apply_image_retention(messages_for_llm)
    messages_for_llm = _apply_adaptive_thinking(messages_for_llm, state.get("session_grants") or [])
    # Repris tel quel depuis l'appel précédent au sein de ce tour (voir
    # AgentState.think_opened/think_closed) plutôt que remis à False, pour ne
    # produire qu'une seule balise <think> continue même si call_llm boucle
    # plusieurs fois via AUTO_APPROVED_TOOLS.
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

    # Ne force la fermeture ici que si ce tour n'ira pas relancer call_llm
    # (pas de tool_calls) : sinon on couperait prématurément un <think>
    # censé continuer sur la prochaine itération de la boucle d'outils
    # auto-approuvés. Le cas "tool_calls + pause d'approbation humaine" est
    # géré séparément côté flux streamé (voir needs_closing_tag, app/main.py).
    if think["opened"] and not think["closed"] and not getattr(merged, "tool_calls", None):
        merged.content += "</think>"
        think["closed"] = True

    # Filet de sécurité (voir MAX_EMPTY_ANSWER_RETRIES plus haut pour la
    # cause racine) : le modèle a parfois écrit son appel d'outil en prose
    # au lieu de le faire reconnaître par la grammaire du serveur. Avant de
    # compter ce tour comme un échec (voir has_tool_calls), on tente de
    # récupérer l'intention plutôt que de perdre le tour.
    if not getattr(merged, "tool_calls", None):
        fallback = _extract_fallback_tool_call(merged.content)
        if fallback:
            logger.warning(
                "Tool call de secours extrait d'une réponse non structurée "
                "(outil=%s, args=%s) : le modèle a écrit son appel en prose "
                "au lieu d'émettre un tool_calls OpenAI reconnu par le serveur.",
                fallback["name"],
                fallback["args"],
            )
            merged.tool_calls = [fallback]

    # Observabilité (Phase 1d-révisée, voir docs/history.md "correctif
    # extraction" -> "OBSERVABILITÉ") : persiste CE tour du modèle
    # (raisonnement <think> + texte + tool_calls éventuels), qu'il soit
    # ensuite auto-approuvé, soumis à approbation ou refusé — contrairement
    # au journal des tool_calls (log_tool_call), volontairement partiel par
    # tier, cette trace-ci n'a pas besoin d'être sélective : c'est le
    # raisonnement de l'agent, jamais un effet de bord à filtrer.
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
        # Remis à False à chaque appel : c'est le seul autre nœud qui
        # termine un tour sur un AIMessage visible (voir
        # AgentState.slash_command_image_shown) — sans cette remise à zéro,
        # un tour LLM normal qui suit une image (ex. vision sur screen_shot
        # décidé par le modèle) réutiliserait à tort la reconstruction
        # d'image de main.py, dupliquant l'image dans sa propre réponse déjà
        # correcte.
        "slash_command_image_shown": False,
    }


def has_tool_calls(state: AgentState) -> str:
    last = state["messages"][-1]
    tool_calls = getattr(last, "tool_calls", None)
    if not tool_calls:
        # Filet de sécurité "réponse vide" (voir MAX_EMPTY_ANSWER_RETRIES) :
        # aucun tool_calls (même après tentative d'extraction de secours
        # dans call_llm) ET rien de visible hors <think> -> reboucle sur
        # call_llm plutôt que d'abandonner immédiatement, tant que le budget
        # de retries n'est pas épuisé.
        if not has_visible_answer(last.content) and state.get("empty_answer_retries", 0) < MAX_EMPTY_ANSWER_RETRIES:
            return "retry_empty_answer"
        return "end"
    if state["tool_iterations"] >= MAX_TOOL_ITERATIONS:
        return "end"
    grants = state.get("session_grants") or []
    all_auto_approved = all(
        approval_policy.is_auto_approved(tc["name"], tc.get("args"), grants) for tc in tool_calls
    )
    # Le garde-fou clavier virtuel (voir AUTO_APPROVAL_STREAK_LIMIT) : même un
    # tour entièrement auto-approuvé repasse par require_approval une fois le
    # plafond de tours consécutifs sans supervision humaine atteint.
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
    """Point de pause : bloque tant qu'un humain n'a pas approuvé/refusé (voir app/main.py)."""
    if state.get("approved") is None:
        raise NodeInterrupt("Approbation humaine requise avant exécution d'outil.")
    # Passage réel par un humain : réarme le budget de tours auto-approuvés
    # consécutifs (voir AUTO_APPROVAL_STREAK_LIMIT).
    updates = {"messages": [], "auto_approval_streak": 0, "grant_session": False}
    # "approuver pour la session" (Phase 3) : les outils du tour en attente
    # rejoignent session_grants, plafonnés à TIER_REVERSIBLE (auto + audit)
    # pour le reste du thread — voir approval_policy.effective_tier() et
    # AgentState.session_grants. Le tour lui-même reste soumis à CETTE
    # approbation (un grant ne s'applique qu'à partir du PROCHAIN appel du
    # même outil, pas rétroactivement à celui qui l'a demandé).
    if state.get("grant_session"):
        last = state["messages"][-1]
        granted_names = {tc["name"] for tc in last.tool_calls}
        updates["session_grants"] = list(set(state.get("session_grants") or []) | granted_names)
    return updates


def route_after_approval(state: AgentState) -> str:
    return "call_tools" if state["approved"] else "reject_tools"


def _to_png_data_uri(data_b64: str, mime_type: str) -> str:
    """
    Réencode systématiquement en PNG avant de transmettre au LLM. Le décodeur
    d'image d'Ollama (mtmd, côté llama.cpp) échoue explicitement sur le WebP
    ("Failed to load image or audio file") — or c'est le format par défaut de
    l'outil screen_shot de GhostDesk. Convertir ici plutôt que de compter sur
    le modèle pour systématiquement demander format="png" à chaque appel.
    Chemin par défaut (IMAGE_FORMAT_PASSTHROUGH non activé) — voir
    _to_image_data_uri pour le chemin WebP direct.
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
    IMAGE_FORMAT_PASSTHROUGH=webp : transmet le WebP brut de screen_shot tel
    quel (data URI directe, aucun décodage/réencodage Pillow), en s'appuyant
    sur le décodage WebP natif du fork llama.cpp servi par le backend
    alternatif llama-server (voir README, section Backend d'inférence) —
    évite le coût CPU de la reconversion PNG à chaque capture. Défaut
    (variable absente/différente de "webp", cas de TabbyAPI comme
    d'Ollama) : conversion PNG systématique via _to_png_data_uri.
    """
    if IMAGE_FORMAT_PASSTHROUGH:
        return f"data:{mime_type};base64,{data_b64}"
    return _to_png_data_uri(data_b64, mime_type)


def _split_image_blocks(result: dict) -> tuple[dict, list[dict]]:
    """
    Sépare les blocs image (format MCP : {"type": "image", "data": <base64>,
    "mimeType": ...}) du reste du résultat d'outil. Un ToolMessage (role
    "tool") ne peut contenir que du texte au format OpenAI-compatible — y
    mettre le base64 brut (via json.dumps sur tout le résultat, comme avant)
    produit un blob texte illisible pour le modèle, image ou pas, multimodal
    ou pas. Les images sont réinjectées séparément en message "user"
    multimodal (voir call_tools), le seul rôle qui supporte un bloc image_url.
    """
    content = result.get("content")
    if not isinstance(content, list):
        return result, []
    images = [b for b in content if isinstance(b, dict) and b.get("type") == "image"]
    if not images:
        return result, []
    rest = [b for b in content if b not in images]
    return {**result, "content": rest or "(voir image ci-dessous)"}, images


async def _call_mcp_tool(client: httpx.AsyncClient, tool_name: str, args: dict) -> tuple[dict, list]:
    """
    Appel HTTP unique à mcp-client:/call, factorisé entre _execute_tool_calls
    (tool_calls décidés par le LLM) et run_slash_command_direct (commande
    tapée directement par l'utilisateur) — même gestion d'erreur/découpage des
    blocs image dans les deux cas.
    """
    try:
        resp = await client.post(
            f"{MCP_CLIENT_URL}/call",
            json={"tool": tool_name, "arguments": args},
        )
        resp.raise_for_status()
        result = resp.json()
    except httpx.HTTPError as exc:
        return {"error": str(exc)}, []
    return _split_image_blocks(result)


async def _execute_tool_calls(state: AgentState, config: dict) -> dict:
    """
    Logique partagée entre call_tools (atteint après require_approval) et
    auto_call_tools (atteint directement depuis has_tool_calls, jamais vu
    par un humain CE tour-ci). Journalise (app/audit_log.py) tout tool_call
    dont le tier effectif n'est pas TIER_READ (silencieux par design, rien
    de nouveau à tracer) — y compris ceux venus de call_tools, quel que
    soit leur tier.

    Angle mort corrigé (voir docs/history.md, investigation T9) : ce nœud
    audit-logguait auparavant SEULEMENT les tool_calls d'auto_call_tools,
    au motif qu'un tour passé par require_approval a déjà sa trace dans
    l'historique de conversation ("⚠️ Approbation requise" + la réponse).
    Ce raisonnement suppose un humain réel qui a vu passer la demande — en
    campagne automatisée, `_approve(..., grant_session=True)` (le harnais)
    joue ce rôle sans qu'aucun humain ne regarde jamais, et l'historique de
    conversation lui-même ne survit pas à un redémarrage du service
    (checkpointer MemorySaver, en mémoire uniquement) : le journal d'audit
    reste alors la SEULE trace persistante, y compris pour le tout premier
    appel de chaque outil par thread — jusqu'ici invisible dans les deux cas.
    """
    last = state["messages"][-1]
    new_messages = []
    grants = state.get("session_grants") or []
    thread_id = config.get("configurable", {}).get("thread_id", "")

    # Garde-fou fabrication d'URL (Phase 1) : périmètre = URL déjà observées
    # CE tour-ci/tours précédents de la tâche + racines du périmètre (1er
    # message humain). Recalculé/étendu au fil des tool_calls DE CE TOUR
    # (plusieurs browser_* peuvent apparaître dans le même tour_calls).
    #
    # Correctif "premier hop" (voir docs/history.md, chantier fiabilité session
    # navigateur) : `has_prior_navigation` distingue le brut persisté
    # (navigations RÉELLEMENT déjà effectuées) de l'union avec
    # `_task_scope_urls` ci-dessous — sert à exempter la toute PREMIÈRE
    # navigation de la tâche du garde-fou (voir plus bas), pas seulement
    # celles vers une URL déjà mentionnée dans le prompt. Root cause : des
    # tâches réelles sans URL dans le prompt (T8 "sur Wikipédia...", T11
    # "quelle est la dernière version de Python ?") voyaient LEUR PREMIÈRE
    # navigation, pourtant légitime, bloquée comme fabrication — confondu
    # au diagnostic avec une panne d'infra playwright-mcp avant de
    # remonter au vrai résultat d'outil (le message de refus du
    # garde-fou lui-même).
    has_prior_navigation = bool(state.get("observed_urls"))
    observed_urls = set(state.get("observed_urls") or []) | _task_scope_urls(state["messages"])
    current_page_url = state.get("current_page_url")
    current_page_links = state.get("current_page_links") or []
    fabricated_attempts = 0
    # Objectif de la tâche (voir _prioritize_affordances) : le 1er message
    # humain, faute de sous-tâches explicites (Phase 1 complète pas encore
    # faite — ce découpage plus fin viendra avec le nœud planificateur).
    first_human = next((m for m in state["messages"] if getattr(m, "type", None) == "human"), None)
    objective = first_human.content if first_human and isinstance(first_human.content, str) else ""

    # Garde-fou "stratégie différente" (Itération 2, voir
    # _repeated_strategy_feedback) : ne s'applique QUE si un échec de
    # vérification a déjà été constaté sur la sous-tâche active (attempts >
    # 0) — un tout premier essai n'a rien à répéter. Comparaison par
    # égalité stricte nom+args (pas de tolérance ε générique sur des
    # schémas d'arguments arbitraires — simplification assumée).
    plan = state.get("plan") or []
    active_index = _active_subtask_index(plan)
    active_attempts = plan[active_index].get("attempts", 0) if active_index is not None else 0
    # state["messages"][-1] EST `last`, le tour COURANT dont les tool_calls
    # sont en train d'être exécutés — exclu de la recherche (messages[:-1])
    # pour que "previous_tool_calls" désigne vraiment le tour PRÉCÉDENT, pas
    # celui-ci (sans quoi tout tool_call se comparerait à lui-même).
    previous_tool_calls = (
        (_previous_turn_tool_calls(state["messages"][:-1]) or []) if VERIFICATION_ENABLED else []
    )

    async with httpx.AsyncClient(timeout=60) as client:
        for tool_call in last.tool_calls:
            if tool_call["name"] == _REPORT_AND_ACT_TOOL_NAME:
                # Meta-outil de repli (correctif latence 1/2-ter, voir
                # _parse_constat) : déjà consommé par verify_action (tourne
                # AVANT ce nœud, sur le même AIMessage) pour muter le plan —
                # jamais dispatché à mcp-client (ça n'est pas un outil MCP
                # réel), jamais audité (TIER_READ, voir
                # approval_policy._DEFAULT_TIER_READ). Un ToolMessage de
                # reçu reste néanmoins obligatoire : chaque tool_call de
                # l'AIMessage précédente doit avoir sa réponse, sans quoi le
                # prochain appel LLM romprait le format OpenAI.
                new_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": json.dumps({"ok": True}, ensure_ascii=False),
                    }
                )
                continue

            # constat_precedent voyage dans les arguments de l'outil réel
            # lui-même (schéma augmenté, voir _inject_constat_param) —
            # retiré ICI, avant tout usage de
            # tool_call["args"] plus bas (dispatch mcp-client, garde-fou
            # anti-fabrication, comparaison anti-répétition, audit). Sans ce
            # retrait, la comparaison stricte nom+args du garde-fou
            # anti-répétition (plus bas) ne matcherait plus JAMAIS deux
            # tentatives par ailleurs identiques (constat différent à chaque
            # fois) — désactivant ce garde-fou silencieusement.
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
                result, images = await _call_mcp_tool(client, tool_call["name"], tool_call["args"])
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
                # Journalisé APRÈS exécution (voir plus haut) pour porter le
                # résultat tel que vu par le modèle (déjà tronqué/hiérarchisé
                # ci-dessus si browser_*) — voir app/audit_log.py, "Phase
                # 1d-révisée".
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

    return {
        "messages": new_messages,
        "tool_iterations": state["tool_iterations"] + 1,
        "approved": None,  # réarme la pause pour le prochain tour d'outils
        # Incrémenté systématiquement (tour auto-approuvé ou juste validé par
        # un humain) : require_approval l'a déjà remis à 0 dans ce second cas,
        # donc cette exécution repart correctement à 1 (voir
        # AUTO_APPROVAL_STREAK_LIMIT).
        "auto_approval_streak": state.get("auto_approval_streak", 0) + 1,
        "observed_urls": sorted(observed_urls),
        "current_page_url": current_page_url,
        "current_page_links": current_page_links,
        "fabricated_navigation_attempts": state.get("fabricated_navigation_attempts", 0) + fabricated_attempts,
        # Une action vient d'être exécutée, verify_action a quelque chose à
        # constater au prochain tour (voir AgentState.pending_verification).
        "pending_verification": True,
    }


async def call_tools(state: AgentState, config: dict) -> dict:
    """Atteint après require_approval (humain ou harnais de campagne vient d'approuver) — voir _execute_tool_calls."""
    return await _execute_tool_calls(state, config)


async def auto_call_tools(state: AgentState, config: dict) -> dict:
    """Atteint directement depuis has_tool_calls (aucune approbation ce tour) — voir _execute_tool_calls."""
    return await _execute_tool_calls(state, config)


async def reject_tools(state: AgentState) -> dict:
    """Miroir de call_tools quand l'humain a refusé : synthétise un refus, n'appelle jamais mcp-client."""
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
    """Index de la sous-tâche "en_cours" du plan, ou None (aucune/plan vide) —
    invariant du plan (Itération 1/2) : au plus une sous-tâche "en_cours" à la fois."""
    return next((i for i, st in enumerate(plan) if st.get("status") == "en_cours"), None)


async def verify_action(state: AgentState, config: dict) -> dict:
    """
    Analyse du constat de vérification post-action (historique des
    révisions successives dans docs/history.md, "correctif latence" — voir aussi
    _verification_directive plus haut). NE FAIT PLUS D'APPEL LLM : le
    verdict est parsé depuis les tool_calls que call_llm vient de produire
    (CE même appel a aussi
    constaté le résultat de l'action précédente ET décidé la suite — voir
    _verification_directive). Ce nœud ne fait que lire ce tool call
    (report_and_act) et mettre à jour le plan en conséquence.

    No-op (`{"messages": []}`) si VERIFICATION_ENABLED est désactivé
    (défaut), s'il n'y a pas de sous-tâche "en_cours", ou si
    `pending_verification` (AgentState) est faux — mêmes conditions que
    _verification_directive, à garder synchronisées : si la consigne n'a
    pas été injectée, il n'y a rien à parser ici non plus. Consomme
    toujours le flag (`pending_verification: False` en retour) : une fois
    constatée, une action ne doit pas être reconstatée au prochain tour si
    aucune NOUVELLE action n'a encore été exécutée entre-temps (ex. tour de
    replanification, qui n'exécute aucun outil).

    Critère vérifié = success_criterion de la sous-tâche ACTIVE du plan.
    Dégradation VOLONTAIREMENT INVERSÉE (voir docs/history.md, "correctif
    latence", pour le score cassé — 18/33 — par la version précédente qui
    traitait un constat absent comme un échec) : constat absent/mal formé
    -> "sans_objet" (NI succès NI échec, budget de tentatives inchangé),
    compté dans constats_inexploitables plutôt que facturé à la sous-tâche.
    Un "sans_objet" légitimement déclaré PAR LE MODÈLE a le même effet sur
    le plan (aucune mutation) mais n'incrémente PAS ce compteur — seule
    l'ambiguïté (constat manquant/mal formé) se mesure.

    Chaque évaluation ici (exploitable ou non) journalise une entrée
    d'audit `role="verification"` avec son verdict d'exploitabilité — juge
    permanent de COUVERTURE (constats exploitables / opportunités),
    compagnon de constats_inexploitables qui ne mesurait que la moitié du
    contrat (l'ambiguïté, pas l'absence pure et simple de tentative). Sans
    ce comptage systématique, une campagne peut afficher
    constats_inexploitables ≈ 0 alors que le taux de couverture réel est
    catastrophique (~9% mesuré sur la campagne qui a motivé ce juge) :
    verify_action ne compte comme "inexploitable" QUE les tentatives
    reconnues comme telles, jamais un constat qui n'a même pas été tenté.
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
            "Sous-tâche %d : constat_precedent absent ou mal formé, constat inexploitable "
            "(sans_objet, budget de tentatives inchangé)",
            active_index,
        )
        return {
            "pending_verification": False,
            "constats_inexploitables": state.get("constats_inexploitables", 0) + 1,
        }

    if verdict == "sans_objet":
        logger.info("Sous-tâche %d : constat sans_objet (rien à mettre à jour)", active_index)
        return {"pending_verification": False}

    new_plan = [dict(st) for st in plan]
    if verdict == "atteint":
        new_plan[active_index]["status"] = "fait"
        new_plan[active_index]["result"] = "critère atteint (constat intégré au tour)"
        if active_index + 1 < len(new_plan):
            new_plan[active_index + 1]["status"] = "en_cours"
        logger.info("Sous-tâche %d atteinte", active_index)
        return {"plan": new_plan, "pending_verification": False}

    # verdict == "non_atteint"
    attempts = new_plan[active_index]["attempts"] + 1
    new_plan[active_index]["attempts"] = attempts
    if attempts < SUBTASK_ATTEMPT_BUDGET:
        logger.info(
            "Sous-tâche %d non atteinte (tentative %d/%d)",
            active_index, attempts, SUBTASK_ATTEMPT_BUDGET,
        )
        return {"plan": new_plan, "pending_verification": False}

    new_plan[active_index]["status"] = "echoue"
    new_plan[active_index]["result"] = "critère non atteint (constat intégré au tour)"
    logger.warning("Sous-tâche %d échouée après %d tentatives", active_index, attempts)
    return {"plan": new_plan, "pending_verification": False}


async def replan_task(state: AgentState) -> dict:
    """
    Replanification (Itération 2) : atteinte quand verify_action a marqué
    une sous-tâche "echoue". Réutilise PLANNER_SYSTEM_PROMPT/
    _validate_plan_json (même schéma que plan_task) avec un prompt de
    contexte (objectif, sous-tâches déjà "fait", raison de l'échec).
    Sous-tâches "fait" préservées telles quelles ; la sous-tâche échouée et
    tout ce qui suivait sont remplacées par la nouvelle décomposition.
    Échec de replanification (LLM/JSON invalide) : repli SANS lever — remet
    juste la sous-tâche échouée à "en_cours"/attempts=0 (nouvelle chance sur
    LE MÊME plan plutôt que de planter). replan_count incrémenté dans tous
    les cas (budget consommé même si la replanification elle-même échoue).
    """
    plan = state.get("plan") or []
    failed_index = next((i for i, st in enumerate(plan) if st.get("status") == "echoue"), None)
    replan_count = state.get("replan_count", 0) + 1
    if failed_index is None:
        return {"replan_count": replan_count}

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
        logger.warning("Replanification échouée, nouvelle tentative sur la même sous-tâche.", exc_info=True)
        new_plan = [dict(st) for st in plan]
        new_plan[failed_index]["status"] = "en_cours"
        new_plan[failed_index]["attempts"] = 0
        return {"plan": new_plan, "replan_count": replan_count}

    rebuilt = [dict(st) for st in plan[:failed_index]]
    for i, st in enumerate(new_subtasks):
        rebuilt.append({**st, "status": "en_cours" if i == 0 else "a_faire", "attempts": 0, "result": None})
    logger.info(
        "Replanification #%d après échec de la sous-tâche %d : %d nouvelle(s) sous-tâche(s)",
        replan_count, failed_index, len(new_subtasks),
    )
    return {"plan": rebuilt, "replan_count": replan_count}


async def report_failure(state: AgentState) -> dict:
    """
    Terminal (Itération 2) : atteint quand une sous-tâche est "echoue" ET le
    budget de replanification (REPLAN_BUDGET) est épuisé. Rapport HONNÊTE de
    l'état atteint — jamais un faux succès, jamais une boucle infinie.
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
    Routage après verify_action (Itération 2, câblage révisé Itération 4 —
    correctif latence 1/2, puis 1/2-bis, voir docs/history.md). verify_action
    tourne maintenant APRÈS call_llm (plus AVANT, voir build_graph) : ce
    routage délègue directement à has_tool_calls (mêmes 4 issues :
    auto_call_tools/call_tools/retry_empty_answer/end), état["messages"][-1]
    restant le même AIMessage tout du long (verify_action ne touche jamais
    "messages").

    Correctif 1/2-bis : le dispatch "sous-tâche echoue -> replan/give_up"
    a été DÉPLACÉ vers route_after_tool_execution (après exécution des
    tool_calls, plus ici avant). Raison : le constat vit désormais dans un
    tool call obligatoire (report_and_act), qui a TOUJOURS besoin d'un
    ToolMessage de reçu pour rester valide au format OpenAI — sauter tout
    droit vers replan_task/report_failure sans exécuter ce tool_calls
    (comme avant, quand le constat vivait dans du texte libre sans jamais
    aucun tool_calls à résoudre) laisserait un tool_call non résolu dans
    l'historique, cassant le prochain appel LLM qui rejoue cet historique.
    """
    return has_tool_calls(state)


def _coerce_slash_arg_value(raw: str):
    """int > float > bool ("true"/"false") > string, dans cet ordre."""
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
    None si le contenu ne commence pas par "/" ou est vide après le "/".
    shlex.split gère les valeurs entre guillemets contenant des espaces. Un
    token sans "=" (argument malformé) est simplement ignoré (log warning)
    plutôt que de faire échouer tout le parsing d'une commande par ailleurs
    valide.
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
            logger.warning("Argument de commande slash ignoré (pas de '=') : %r", tok)
            continue
        key, _, raw_value = tok.partition("=")
        args[key] = _coerce_slash_arg_value(raw_value)
    return tool_name, args


def _format_tool_result_as_text(result: dict) -> str:
    """Extrait le texte des blocs {"type": "text", ...} du résultat d'outil ;
    à défaut (résultat vide, erreur, forme inattendue), JSON indenté brut."""
    blocks = result.get("content", []) if isinstance(result, dict) else []
    if isinstance(blocks, str):
        # _split_image_blocks retombe sur ce placeholder textuel quand TOUS
        # les blocs du résultat étaient des images (ex. screen_shot seul) —
        # ce n'est déjà pas une liste de blocs, le renvoyer tel quel plutôt
        # que d'itérer sur ses caractères (aucun n'est un dict "text", donc
        # ça retombait silencieusement sur un dump JSON de tout le dict).
        return blocks
    texts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
    if texts:
        return "\n".join(texts)
    return json.dumps(result, ensure_ascii=False, indent=2)


async def prepare_slash_command(state: AgentState, config: dict) -> dict:
    """
    Parse la commande slash et synthétise le tool_calls correspondant, sans
    encore l'exécuter — le routage par tier (_route_slash_command_tier)
    décide ensuite si ça part en direct (run_slash_command_direct) ou par la
    vraie pause d'approbation (require_approval), selon le tier de l'outil.
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
    GARDE-FOU : une commande slash sur un outil TIER_SENSITIVE (ex. key_type
    avec texte long, clipboard_get) ne s'exécute PAS directement — elle part
    par require_approval, exactement comme un tool_calls décidé par le LLM.
    Le fait de taper explicitement la commande ne vaut approbation que pour
    TIER_READ/TIER_REVERSIBLE : le tier sensible existe précisément pour
    imposer une confirmation séparée avant une action potentiellement
    dangereuse (texte libre tapé dans un terminal, exfiltration du
    presse-papier...) — un bypass total aurait annulé cette garantie pour
    n'importe quel outil, y compris ceux jamais voulus auto-approuvés.
    """
    last = state["messages"][-1]
    tool_call = last.tool_calls[0]
    grants = state.get("session_grants") or []
    tier = approval_policy.effective_tier(tool_call["name"], tool_call.get("args"), grants)
    return "sensitive" if tier == approval_policy.TIER_SENSITIVE else "direct"


async def run_slash_command_direct(state: AgentState, config: dict) -> dict:
    """
    Exécute directement le tool_calls synthétisé par prepare_slash_command
    (tier lecture/réversible uniquement, voir _route_slash_command_tier) —
    ni LLM ni pause d'approbation. Termine sur un AIMessage de forme
    standard (pas juste le ToolMessage brut) pour rester compatible sans
    aucune modification avec main.py, qui suppose que le dernier message
    d'un tour terminé est un AIMessage avec du contenu visible (voir
    _stream_response/_current_answer, qui basculeraient sinon sur la notice
    "réponse non exploitable").
    """
    last = state["messages"][-1]
    tool_call = last.tool_calls[0]
    tool_name, args, call_id = tool_call["name"], tool_call["args"], tool_call["id"]

    # Traçabilité uniquement (parité avec auto_call_tools) : n'influence
    # jamais l'exécution — le tier sensible a déjà été écarté par
    # _route_slash_command_tier avant d'arriver ici.
    grants = state.get("session_grants") or []
    tier = approval_policy.effective_tier(tool_name, args, grants)
    thread_id = config.get("configurable", {}).get("thread_id", "")

    async with httpx.AsyncClient(timeout=60) as client:
        result, images = await _call_mcp_tool(client, tool_name, args)

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
    # Le message "user" ci-dessus (bloc image_url standard) est ce que voit
    # un futur tour LLM sur ce thread — format efficace pour un modèle
    # multimodal (coût fixe par image côté API), PAS de base64 embarqué en
    # texte brut dans le message assistant final : un essai précédent
    # embarquait l'image en markdown directement ici, ce qui la faisait
    # certes apparaître dans CETTE réponse, mais la persistait aussi dans
    # l'historique sous forme de texte — tokenisée comme du texte ordinaire
    # (des dizaines de milliers de tokens pour une seule capture) au lieu du
    # coût fixe d'un vrai bloc image_url, faisant exploser le contexte
    # (32768 tokens dépassés) dès le tour LLM suivant sur ce thread, même
    # avec une seule image (MAX_IMAGES_IN_CONTEXT=1 ne trimme jamais LA
    # dernière image, donc aucune protection possible sous cette forme).
    # L'affichage de l'image POUR CE TOUR est reconstruit côté main.py
    # (_render_visible_answer) à partir de ce message "user" séparé, jamais
    # en la persistant une seconde fois ici.
    new_messages.append({"role": "assistant", "content": _format_tool_result_as_text(result)})

    return {
        "messages": new_messages,
        "tool_iterations": state["tool_iterations"] + 1,
        "slash_command_image_shown": bool(images),
    }


async def _route_entry(state: AgentState) -> str:
    """
    Point d'entrée conditionnel du graphe : bascule sur prepare_slash_command
    si le dernier message est une commande slash dont le nom d'outil est
    CONNU (_tools_schema_cache, format OpenAI function-calling imbriqué
    {"function": {"name": ...}}, voir mcp-client:/tools/schema) — un message
    qui commence juste par "/" sans être une commande valide (ex. un chemin
    de fichier) suit le flux normal plutôt que de déclencher une erreur 404
    confuse pour un nom qui n'était jamais censé être un outil.
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
    Routage après call_tools/auto_call_tools (correctif latence 1/2-bis,
    voir route_after_verification pour la raison du déplacement) :

    1. Sous-tâche "echoue" (verify_action vient de la marquer, budget de
       tentatives épuisé, voir plus haut) -> replan_task/report_failure —
       le tool_calls du tour (report_and_act au minimum) vient d'être
       exécuté juste avant, donc déjà résolu par un ToolMessage, quel que
       soit le chemin choisi ensuite.
    2. Court-circuit sinon : si le SEUL tool_calls du tour était
       report_and_act (aucune action réelle décidée) ET que ce même tour
       portait déjà une réponse visible (cas fréquent : dernière sous-tâche
       atteinte, réponse finale donnée dans le même tour que son constat),
       reboucler sur call_llm coûterait un appel LLM entier pour ne faire
       répéter au modèle qu'une réponse déjà produite — exactement le coût
       que ce chantier cherche à éliminer. Route vers finalize_after_report
       plutôt que directement END (voir ce nœud : sans lui, le dernier
       message du thread serait le ToolMessage de reçu de report_and_act,
       pas la réponse visible — cassant _current_answer/app/main.py, qui
       suppose partout que messages[-1] est l'AIMessage de réponse).
    3. Cas normal (une vraie action a aussi été exécutée, ou aucune réponse
       visible) : comportement inchangé, retour à call_llm.
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
    Voir route_after_tool_execution ("finalize") : ré-émet le texte de la
    réponse déjà produite (et déjà streamée au client par call_llm) comme un
    NOUVEL AIMessage propre, SANS tool_calls — pour que messages[-1] reste
    l'AIMessage de réponse visible, pas le ToolMessage de reçu de
    report_and_act qui vient d'être exécuté. Même précédent que
    run_slash_command_direct (voir sa docstring) : aucun appel LLM, un
    simple message de forme standard pour rester compatible avec
    app/main.py.
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
    # verify_action tourne APRÈS call_llm (analyse du constat que ce même
    # appel vient de produire, voir docs/history.md "correctif latence") —
    # route_after_verification délègue à has_tool_calls
    # (call_tools/auto_call_tools/retry_empty_answer/end). Le dispatch
    # replan/give_up sur sous-tâche "echoue" vit dans
    # route_after_tool_execution, pas ici.
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
    # reject_tools résout aussi TOUS les tool_calls du tour (dont
    # report_and_act, voir reject_tools) — même routage post-exécution que
    # call_tools/auto_call_tools, pour ne jamais sauter par-dessus un
    # échoue/give_up potentiellement posé par verify_action juste avant
    # (correctif latence 1/2-bis : ce cas n'était pas exercé par les tests
    # avant que report_and_act rende un tool_calls quasi systématique sur
    # les tours vérifiés).
    graph.add_conditional_edges(
        "reject_tools",
        route_after_tool_execution,
        {"call_llm": "call_llm", "finalize": "finalize_after_report_and_act", "replan": "replan_task", "give_up": "report_failure"},
    )
    graph.add_edge("retry_empty_answer", "call_llm")

    return graph.compile(checkpointer=checkpointer or MemorySaver())


agent_graph = build_graph()
