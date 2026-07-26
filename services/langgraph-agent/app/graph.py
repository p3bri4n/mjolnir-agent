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

# Schema of the MCP tools (terminal/filesystem/git/browser/desktop-GhostDesk),
# fetched from mcp-client and cached for the process's lifetime. Without
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
    if not schema:
        return llm
    if not VERIFICATION_ENABLED:
        return llm.bind_tools(schema)
    # constat_precedent injected as a required parameter of EVERY real MCP
    # tool, plus report_and_act as the sole fallback (pure-text turn, no
    # action) — see above. Gated on VERIFICATION_ENABLED: without it, this
    # field has no reader (_verification_directive doesn't instruct it)
    # and would only add noise to the schema sent to the model.
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


async def plan_task(state: AgentState) -> dict:
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
    return {"plan": plan}


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


async def validate_plan(state: AgentState) -> dict:
    """
    Plan validation pipeline (Iteration 3, Phase 1 "cognitive core").
    No-op (`{"messages": []}`) if PLAN_VALIDATION_ENABLED is disabled
    (default) or if `state["plan"]` is empty — same behavior as before
    this iteration. Otherwise: programmatic heuristics
    (app/plan_validation.py, free) then, ONLY if they pass AND
    PLAN_JUDGE_ENABLED, LLM judge (costly — withdrawal clause, see
    docs/history.md). Rejection (heuristics OR judge) -> plan_validation_cycles
    incremented, reasons returned for route_after_validation.
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
    return {"plan": plan}


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
    (GROUNDING_DIRECTIVE): nothing has been composed yet for this thread.
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
    GhostDesk perception-action loop (capture -> click -> capture) where
    Qwen3.6's extended reasoning costs more than it's worth. No injection
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
    # GROUNDING_DIRECTIVE, added by call_llm right before this call),
    # otherwise inserted at position 0 — never at the end of the list:
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
        # model-decided screen_shot) would wrongly reuse main.py's image
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
