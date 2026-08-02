"""
Exposes an OpenAI-compatible API (/v1/chat/completions) consumed by Open
WebUI, delegating internally to the LangGraph graph (app/graph.py).
Supports token-by-token SSE streaming via astream_events.

Human supervision: every tool call suspends the graph (see
require_approval in app/graph.py) until the user replies
"approuver"/"refuser" on the next conversation turn. Since Open WebUI
resends the full history on every request with no stable conversation
identifier, the LangGraph thread is recovered by deriving a deterministic
thread_id from the first human message (see _derive_thread_id) — two
conversations starting with a strictly identical message would therefore
share the same thread, an accepted limitation for local single-user use.
"""

import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app import audit_log
from app.graph import (
    MAX_TOOL_ITERATIONS,
    _get_tools_schema,
    _plan_tier,
    agent_graph,
    describe_context,
    has_visible_answer,
)

app = FastAPI(title="LangGraph Agent")
logger = logging.getLogger(__name__)


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = "agent-llm"
    messages: List[ChatMessage]
    stream: Optional[bool] = False


class PendingCheckRequest(BaseModel):
    """Only needs to derive the same thread_id as everything else (see
    _derive_thread_id, based solely on the first human message) — hence
    indifferent to whether the last assistant message's content, as
    returned by the client, might be empty or truncated (observed with
    Open WebUI on messages containing <think> tags, independent of this
    service: its displayed value and its internally stored value can
    diverge). Lets a client (UI button) know whether an approval is
    pending without depending on that content."""

    messages: List[ChatMessage]


class ContextRequest(BaseModel):
    """
    POST /context (observability dashboard, services/dashboard): accepts
    either `messages` (same contract as PendingCheckRequest, thread_id
    derived via _derive_thread_id), or `thread_id` directly (Phase 3 — the
    dashboard fetches it via GET /threads/recent rather than replaying the
    whole Open WebUI history it never had in the first place). `thread_id`
    takes precedence if provided."""

    messages: Optional[List[ChatMessage]] = None
    thread_id: Optional[str] = None


class ApprovalDecisionRequest(BaseModel):
    """Decision passed out-of-band, from a UI button (Open WebUI Action
    function) rather than via an "approuver"/"refuser" text message — see
    /approve. `messages` must be the full history as seen by Open WebUI at
    click time (same contract as ChatCompletionRequest.messages), needed
    to derive the same thread_id and keep owui_message_count up to date
    the same way the existing text flow does."""

    messages: List[ChatMessage]
    approved: bool
    # "approve for the session" (Phase 3): grants the tool for the whole
    # thread rather than just this turn — see AgentState.session_grants,
    # app/graph.py. Ignored if approved=False.
    grant_session: bool = False


def _derive_thread_id(messages: List[ChatMessage]) -> str:
    first_human = next((m.content for m in messages if m.role == "user"), "")
    return hashlib.sha256(first_human.encode()).hexdigest()[:16]


# Process-in-memory registry (Phase 3, never persisted — consistent with
# the MemorySaver checkpointer itself being in-memory only, see README
# Data persistence section) of recently seen threads, so the
# observability dashboard (services/dashboard) can call POST /context
# without having to replay the full Open WebUI history, which it never
# received in the first place. Fed only by the endpoints that actually
# advance a conversation (_resolve_run, /approve) — not by /pending or
# /context themselves, strictly read-only.
_recent_threads: dict = {}


def _touch_thread(thread_id: str) -> None:
    _recent_threads[thread_id] = datetime.now(timezone.utc).isoformat()


_PLAN_STATUS_LABELS = {"a_faire": "à faire", "en_cours": "en cours", "fait": "fait", "echoue": "échoué"}


def _format_plan_summary(plan: Optional[list]) -> str:
    """
    Plan summary (Iteration 1, Phase 1 "cognitive core" — see
    docs/briefs/phase-1-coeur-cognitif.md and app/graph.py:plan_task) for
    the approval message. Empty/None `plan` -> empty string
    (PLANNER_ENABLED disabled by default, see app/graph.py): changes
    NOTHING to the existing text then, so as not to break any test that
    checks this message today.
    """
    if not plan:
        return ""
    lignes = ["Plan de la tâche :"]
    for i, sous_tache in enumerate(plan, 1):
        label = _PLAN_STATUS_LABELS.get(sous_tache.get("status"), sous_tache.get("status", "?"))
        lignes.append(
            f"{i}. [{label}] {sous_tache.get('description', '')} "
            f"(critère : {sous_tache.get('success_criterion', '')})"
        )
    return "\n".join(lignes)


def _format_approval_request(tool_calls: list, plan: Optional[list] = None) -> str:
    demandes = ", ".join(f'`{tc["name"]}`({tc["args"]})' for tc in tool_calls)
    base = (
        f'⚠️ Approbation requise pour : {demandes}. Réponds "approuver" (une fois), '
        f'"approuver pour la session" (pour ne plus être sollicité sur ce(s) outil(s) '
        f"tant que dure cette conversation) ou \"refuser\" pour continuer."
    )
    plan_summary = _format_plan_summary(plan)
    return f"{base}\n\n{plan_summary}" if plan_summary else base


def _format_plan_approval_request(plan: list, tier: str, reasons: Optional[list] = None) -> str:
    """
    PLAN approval message (Iteration 3, app/graph.py:
    require_plan_approval) — distinct from _format_approval_request
    (approval of a specific tool_call). Two cases: normal tier-based
    approval (`reasons` empty, the plan passed validation) or human
    escalation after repeated automatic validation failure (`reasons`
    non-empty — reasons displayed, see route_after_validation).
    """
    if reasons:
        header = (
            "⚠️ Le plan proposé a été rejeté par la validation automatique après "
            "plusieurs tentatives — décision humaine requise. Motifs :\n"
            + "\n".join(f"- {r}" for r in reasons)
        )
    else:
        header = f"⚠️ Approbation du plan requise (tier : {tier})."
    footer = 'Réponds "approuver" (une fois), "approuver pour la session" ou "refuser".'
    summary = _format_plan_summary(plan)
    return f"{header}\n\n{summary}\n\n{footer}" if summary else f"{header}\n\n{footer}"


def _pending_approval_text(snapshot) -> Optional[str]:
    """
    Text of the current approval pause for this snapshot, or None if
    there isn't one. Centralizes the PLAN pause (require_plan_approval,
    Iteration 3) vs TOOL pause (require_approval, existing) distinction
    already introduced in _resolve_run — avoids duplicating it at the 4
    places that display this text (streaming, _current_answer, /pending,
    /context).
    """
    if not snapshot.next:
        return None
    if "require_plan_approval" in snapshot.next:
        plan = snapshot.values.get("plan") or []
        reasons = snapshot.values.get("plan_validation_reasons") or []
        return _format_plan_approval_request(plan, _plan_tier(plan), reasons)
    messages = snapshot.values.get("messages") or []
    if not messages or not getattr(messages[-1], "tool_calls", None):
        return None
    return _format_approval_request(messages[-1].tool_calls, snapshot.values.get("plan"))


def _parse_approval_reply(text: str) -> tuple:
    """
    Distinguishes the three possible replies to the approval message (see
    _format_approval_request): since "approuver pour la session" itself
    contains "approuver", the grant is detected by looking for "session"
    IN ADDITION to "approuver" — a plain "approuver" never grants
    anything.
    """
    lowered = text.lower()
    approved = "approuver" in lowered
    grant_session = approved and "session" in lowered
    return approved, grant_session


_INTERNAL_ERROR_NOTICE = "⚠️ Erreur interne pendant la génération, réessayez."


def _format_iteration_limit_notice(tool_calls: list) -> str:
    demandes = ", ".join(f'`{tc["name"]}`({tc["args"]})' for tc in tool_calls)
    return (
        f"⚠️ Limite d'itérations d'outils atteinte pour cette tâche avant d'avoir pu exécuter : "
        f"{demandes}. Envoie un nouveau message pour relancer une tâche fraîche."
    )


def _format_empty_answer_notice() -> str:
    """
    Non-regression (real bug observed in real usage, see the README's bug
    table): a model can end a turn with no structured tool_calls AND no
    visible answer text — e.g. a tool-call attempt written in prose
    (mimicking the <tool_call> syntax it sees rendered by the template for
    its own previous turns) buried in the reasoning, never recognized as
    an OpenAI tool_calls. Without this message, the user only sees the
    reasoning bubble close on nothing: the same "the agent seems to stop
    mid-task" symptom as MAX_TOOL_ITERATIONS (see
    _format_iteration_limit_notice), but via a different path (no
    tool_calls pending, just an empty answer).
    """
    return (
        "⚠️ Le modèle a terminé son tour sans réponse exploitable (probablement une "
        "tentative d'appel d'outil restée noyée dans son raisonnement plutôt que d'être "
        "émise comme un vrai appel d'outil structuré). Envoie un nouveau message pour réessayer."
    )


async def _resolve_run(request: ChatCompletionRequest):
    """
    Prepares the (config, run_input) to pass to the graph.

    Three cases, distinguished via the state persisted by the
    checkpointer for this thread:
      - an approval pause is in progress -> inject the decision and
        resume (run_input=None);
      - the thread already exists (conversation in progress, previous
        turns already persisted) -> Open WebUI resends the FULL history
        on every request, but this thread has already persisted previous
        turns via the checkpointer; submitting only the new messages
        (beyond owui_message_count) avoids duplicating the whole
        already-stored history;
      - this conversation's very first turn -> no state persisted yet,
        submit the initial history as-is.
    """
    # recursion_limit counts the NODES visited (not tool calls) and
    # defaults to 25 on the LangGraph side — independent of
    # MAX_TOOL_ITERATIONS and reached much faster: an auto-approved
    # GhostDesk loop can chain many call_llm/call_tools turns without ever
    # going back through an approval pause, which would otherwise split
    # the run into several ainvoke() calls each with a fresh recursion
    # budget. Without this adjustment, a long enough auto-approved run
    # raises a raw GraphRecursionError (500) before even reaching our own
    # limit notice (see _format_iteration_limit_notice above).
    thread_id = _derive_thread_id(request.messages)
    _touch_thread(thread_id)
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": MAX_TOOL_ITERATIONS * 4 + 10,
    }
    snapshot = await agent_graph.aget_state(config)
    # Number of Open WebUI messages this turn will fully cover once its
    # (single) answer is produced: the current history + this answer.
    owui_message_count = len(request.messages) + 1

    if snapshot.next:
        last_human = next(
            (m.content for m in reversed(request.messages) if m.role == "user"), ""
        )
        approved, grant_session = _parse_approval_reply(last_human)
        # Two possible pause reasons since Iteration 3 (plan validation
        # pipeline): require_plan_approval (the PLAN) or require_approval
        # (a tool_call), never both at once (distinct graph nodes) —
        # snapshot.next holds the interrupted node's name, enough to tell
        # them apart with no extra state.
        if "require_plan_approval" in snapshot.next:
            await agent_graph.aupdate_state(
                config,
                {
                    "plan_approved": approved,
                    "plan_grant_session": grant_session,
                    "owui_message_count": owui_message_count,
                },
            )
        else:
            await agent_graph.aupdate_state(
                config,
                {"approved": approved, "grant_session": grant_session, "owui_message_count": owui_message_count},
            )
        return config, None

    already_seen = snapshot.values.get("owui_message_count", 0) if snapshot.values else 0
    new_messages = request.messages[already_seen:]

    run_input = {
        "messages": [{"role": m.role, "content": m.content} for m in new_messages],
        "tool_iterations": 0,
        "approved": None,
        "owui_message_count": owui_message_count,
        "think_opened": False,
        "think_closed": False,
        "auto_approval_streak": 0,
        # session_grants is DELIBERATELY absent here (2026-07-31, see
        # docs/resolved-bugs.md "session_grants remis à zéro par tour") —
        # every other field on this dict resets per top-level turn, but a
        # session grant is documented (AgentState.session_grants,
        # README's "reversible writes are covered by a session grant")
        # to last the rest of the THREAD, not one turn. Omitting the key
        # from a partial state update leaves the checkpointer's existing
        # value untouched; a brand-new thread naturally reads back []
        # via the state.get("session_grants") or [] pattern used
        # everywhere it's consumed (app/graph.py).
        "grant_session": False,
        "empty_answer_retries": 0,
        "slash_command_image_shown": False,
        "observed_urls": [],
        "current_page_url": None,
        "current_page_links": [],
        "fabricated_navigation_attempts": 0,
        "plan": [],
        "subtask_message_start": [],
        "replan_count": 0,
        "plan_validation_reasons": [],
        "plan_validation_cycles": 0,
        "plan_approved": None,
        "plan_grant_session": False,
        "plan_grant": False,
        "pending_verification": False,
        "constats_inexploitables": 0,
    }
    return config, run_input


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models():
    # needed so Open WebUI discovers the agent "model"
    return {
        "object": "list",
        "data": [{"id": "agent-llm", "object": "model", "owned_by": "langgraph-agent"}],
    }


def _sse_chunk(completion_id: str, model: str, delta: dict, finish_reason: Optional[str] = None) -> str:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# Max size of a content piece per SSE chunk. Needed since some content
# sent as a single block (notices, and especially a base64 data-URI image
# for a slash command on a tool like screen_shot — see app/graph.py,
# run_slash_command_direct) can far exceed the size of a normal LLM
# streaming token. Without this splitting, an HTTP client with a
# line-size limit (e.g. aiohttp on the Open WebUI side, 131072 bytes by
# default) rejects the response wholesale with an unhelpful error ("Got
# more than 131072 bytes when reading") instead of receiving it in
# several small pieces like real token-by-token streaming would.
_SSE_CONTENT_CHUNK_SIZE = 8192


def _sse_content_chunks(completion_id: str, model: str, content: str):
    if not content:
        return
    for i in range(0, len(content), _SSE_CONTENT_CHUNK_SIZE):
        piece = content[i : i + _SSE_CONTENT_CHUNK_SIZE]
        delta = {"role": "assistant", "content": piece} if i == 0 else {"content": piece}
        yield _sse_chunk(completion_id, model, delta)


async def _stream_response(config: dict, run_input: Optional[dict], model: str):
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    sent_role = False
    streamed_text = []

    try:
        # Only forwards content tokens to the client (on_chat_model_stream).
        # Iterations that decide on a tool call have empty content on the
        # LLM side (the tool_call arrives on a separate channel), so
        # nothing visible is sent while tools are being resolved: only the
        # final answer appears, token by token. If the graph pauses for
        # approval, no token is emitted by this mechanism (see below).
        async for event in agent_graph.astream_events(run_input, config, version="v2"):
            if event["event"] != "on_chat_model_stream":
                continue
            chunk = event["data"]["chunk"]
            if not chunk.content:
                continue
            streamed_text.append(chunk.content)
            if not sent_role:
                yield _sse_chunk(completion_id, model, {"role": "assistant", "content": chunk.content})
                sent_role = True
            else:
                yield _sse_chunk(completion_id, model, {"content": chunk.content})

        # If the model reasoned before deciding to call a tool, the
        # <think>...</think> tokens streamed above (see app/graph.py) never
        # got their closing tag: on the LLM side, a turn that ends in a
        # tool_call has empty final content, so no "real" content chunk
        # ever arrives to trigger the closing (see
        # _convert_delta_with_reasoning). call_llm does close the tag on
        # the PERSISTED message afterward, but that doesn't fix the chunks
        # already sent to the client in the loop above — so it's what was
        # actually streamed (`streamed_text`) that must be checked, not
        # the already-repaired state. Without this fix, the text added
        # next (approval pause or limit notice) ends up swallowed inside
        # the <think> that stayed open on the client side, invisible
        # outside the collapsed bubble.
        full_streamed = "".join(streamed_text)
        closing_prefix = "</think>\n\n" if full_streamed.count("<think>") > full_streamed.count("</think>") else ""
        # closing_prefix closes the tag on the client side, but
        # AgentState.think_closed (app/graph.py) stays False since call_llm
        # couldn't close it itself (tool_calls present). Without this
        # update, a resumption after approval would restart with
        # think_opened=True/think_closed=False: a new reasoning round
        # would then receive no opening tag (already "opened" per
        # persisted state) but would still receive a closing tag at the
        # end of the turn — an orphaned </think> visible on the client
        # side, with no matching <think> in what it received.
        if closing_prefix:
            await agent_graph.aupdate_state(config, {"think_opened": False, "think_closed": False})

        snapshot = await agent_graph.aget_state(config)
        if snapshot.next:
            pending = closing_prefix + _pending_approval_text(snapshot)
            for chunk in _sse_content_chunks(completion_id, model, pending):
                yield chunk
        else:
            last_message = snapshot.values["messages"][-1]
            if getattr(last_message, "tool_calls", None):
                # The graph stopped on MAX_TOOL_ITERATIONS with a tool_call
                # still pending on the model side: without this message,
                # the agent just seems to "stop" mid-task, with no error
                # or approval pause explaining it (see MAX_TOOL_ITERATIONS,
                # app/graph.py).
                notice = closing_prefix + _format_iteration_limit_notice(last_message.tool_calls)
                for chunk in _sse_content_chunks(completion_id, model, notice):
                    yield chunk
            elif not has_visible_answer(full_streamed + closing_prefix):
                if has_visible_answer(last_message.content):
                    # The PERSISTED final message does have a visible
                    # answer, but it never went through on_chat_model_stream
                    # above (e.g. a slash command — app/graph.py,
                    # run_slash_command_direct — which never invokes the
                    # LLM and hence produces no content chunk here).
                    # Without this case, this answer, though genuinely
                    # present in state, would be wrongly replaced by the
                    # "réponse non exploitable" notice below, which
                    # assumes nothing streamed AND nothing exists. Split
                    # into several chunks (_sse_content_chunks): may
                    # contain a base64 data-URI image (screen_shot via a
                    # slash command), far above the line-size limit of
                    # some HTTP clients (aiohttp on the Open WebUI side).
                    visible = _render_visible_answer(snapshot.values)
                    for chunk in _sse_content_chunks(completion_id, model, visible):
                        yield chunk
                else:
                    # See _format_empty_answer_notice: no tool_calls
                    # pending AND nothing visible outside <think>, neither
                    # here nor in the persisted message — same "silent
                    # agent" symptom as above, different cause.
                    notice = closing_prefix + _format_empty_answer_notice()
                    for chunk in _sse_content_chunks(completion_id, model, notice):
                        yield chunk
    except Exception:
        # Without this safety net, an error here (llama-server cutting the
        # connection mid-stream, checkpointer unavailable...) kills this
        # generator in the middle of an already-started
        # "Transfer-Encoding: chunked" response: uvicorn then closes the
        # connection without ever sending the terminal chunk, and the
        # client (e.g. aiohttp on the Open WebUI side) fails with
        # "TransferEncodingError: Not enough data to satisfy transfer
        # length header" — a client-side symptom of a server-side crash,
        # not a client bug. Instead, a visible notice is returned and the
        # SSE stream is closed cleanly below.
        logger.exception(
            "Error during SSE streaming (thread_id=%s)", config["configurable"]["thread_id"]
        )
        yield _sse_chunk(
            completion_id,
            model,
            {"role": "assistant", "content": _INTERNAL_ERROR_NOTICE},
        )

    yield _sse_chunk(completion_id, model, {}, finish_reason="stop")
    yield "data: [DONE]\n\n"


def _render_visible_answer(snapshot_values: dict) -> str:
    """
    Reconstructs the final visible text for THIS turn from persisted
    state — if slash_command_image_shown is true (see app/graph.py,
    run_slash_command_direct: a slash command on an image-only tool, e.g.
    /screen_shot), adds the image from the preceding "human" message as
    markdown to the answer returned HERE ONLY. Never persisted in this
    form: the stored assistant message stays light (text only), so as not
    to re-tokenize the base64 as raw text on a future LLM turn on this
    thread — without this separation, a single screenshot
    (MAX_IMAGES_IN_CONTEXT=1 never trims THE last image) was enough on its
    own to exceed 32768 tokens as early as the next LLM turn (real bug
    observed via Open WebUI). The explicit slash_command_image_shown
    signal (rather than guessing from message shape) is needed: a normal
    LLM turn that itself analyzed an image via vision also produces an
    AIMessage right after an image message, without needing the image
    added a second time (it's already correctly described).
    """
    messages = snapshot_values["messages"]
    text = messages[-1].content
    if snapshot_values.get("slash_command_image_shown") and len(messages) >= 2:
        prev = messages[-2]
        if getattr(prev, "type", None) == "human" and isinstance(prev.content, list):
            image_urls = [
                b["image_url"]["url"] for b in prev.content if isinstance(b, dict) and b.get("type") == "image_url"
            ]
            if image_urls:
                images_md = "\n".join(f"![résultat outil]({url})" for url in image_urls)
                text = f"{text}\n\n{images_md}" if text else images_md
    return text


async def _current_answer(config: dict) -> str:
    snapshot = await agent_graph.aget_state(config)
    if snapshot.next:
        return _pending_approval_text(snapshot)
    last_message = snapshot.values["messages"][-1]
    if getattr(last_message, "tool_calls", None):
        return _format_iteration_limit_notice(last_message.tool_calls)
    if not has_visible_answer(last_message.content):
        return _format_empty_answer_notice()
    return _render_visible_answer(snapshot.values)


@app.post("/pending")
async def pending(request: PendingCheckRequest):
    """Read-only: never invokes the graph, never modifies any state."""
    config = {"configurable": {"thread_id": _derive_thread_id(request.messages)}}
    snapshot = await agent_graph.aget_state(config)
    if not snapshot.next:
        return {"pending": False}
    return {"pending": True, "text": _pending_approval_text(snapshot)}


@app.get("/tools/schema")
async def tools_schema():
    """
    Read-only (same convention as /pending): tool names as ACTUALLY seen
    by this langgraph-agent process (_tools_schema_cache, see
    app/graph.py), not those served by mcp-client at call time — the
    distinction has bitten under real conditions (revised Phase 1d, see
    docs/history.md "tool-schema cache bug"): _tools_schema_cache is
    filled once for the process's lifetime and never invalidated, so
    restarting mcp-client alone (schema updated server-side) can leave
    this endpoint answering with a stale schema until langgraph-agent
    itself restarts. Exists so an external caller (test harness,
    dashboard) can detect this gap rather than discovering it after the
    fact in a failed run.
    """
    schema = await _get_tools_schema()
    names = sorted({t.get("function", {}).get("name") for t in schema if t.get("function", {}).get("name")})
    return {"tools": names}


@app.post("/context")
async def context(request: ContextRequest):
    """
    Read-only (same convention as /pending, no side effect): approximate
    breakdown of the context persisted for this thread, for use by the
    observability dashboard (services/dashboard, POST /api/snapshot). See
    describe_context (app/graph.py) for the block details. Explicit
    thread_id (Phase 3, via GET /threads/recent) or derived from
    `messages` like /pending. No state for this thread (thread_id unknown
    to the checkpointer, or never provided) -> 200 with empty blocks
    rather than a 404: the dashboard polls this endpoint continuously, a
    transient 404 (e.g. right before a conversation's very first message)
    would just be noise for the client to handle.
    """
    thread_id = request.thread_id or _derive_thread_id(request.messages or [])
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = await agent_graph.aget_state(config)
    messages = snapshot.values.get("messages", []) if snapshot.values else []

    pending_text = _pending_approval_text(snapshot)

    blocks = describe_context(messages, pending_text)
    return {
        "blocks": blocks,
        "total_est_tokens": sum(b["est_tokens"] for b in blocks),
        "message_count": len(messages),
    }


@app.get("/threads/recent")
async def threads_recent():
    """
    Recently seen threads (Phase 3, see _recent_threads above): the 5
    most recent, sorted newest to oldest — feeds the observability
    dashboard's (services/dashboard) dropdown menu, which otherwise has
    no way of knowing which thread to query via POST /context.
    """
    ordered = sorted(_recent_threads.items(), key=lambda item: item[1], reverse=True)[:5]
    return {"threads": [{"thread_id": tid, "last_seen": last_seen} for tid, last_seen in ordered]}


@app.get("/audit")
async def audit(thread_id: Optional[str] = None):
    """
    Reads the audit log (Phase 2, app/audit_log.py): TIER_REVERSIBLE
    tool_calls actually executed (auto-approved or granted for the
    session). Without thread_id, returns the whole available log (across
    all daily files); with thread_id, returns only that thread's entries.
    """
    return {"entries": audit_log.read_entries(thread_id)}


@app.post("/approve")
async def approve(request: ApprovalDecisionRequest):
    """
    Resumes a thread paused for approval directly from an out-of-band
    decision (UI button), without going through the "approuver"/
    "refuser" text message _resolve_run normally expects.

    owui_message_count bookkeeping (see _resolve_run) auto-detects which
    of two client conventions sent `request.messages`, rather than
    assuming one (2026-07-31 fix, see docs/resolved-bugs.md): Open WebUI's
    own Action button edits the existing "⚠️ Approbation requise" message
    IN PLACE with the final answer — its `messages` therefore already
    includes a placeholder for this turn, matching the count stored at
    pause time (`_resolve_run`'s own `+1`), with no further growth to
    anticipate. A programmatic client that instead APPENDS a brand new
    assistant message once this call resolves (no in-place edit) sends
    `messages` one shorter than that stored count — anticipate its
    upcoming growth instead, or the next turn's
    `request.messages[already_seen:]` split would re-inject this turn's
    already-answered content as if it were new. Both conventions are
    detected from `len(request.messages)` alone, compared against the
    count already persisted for this thread — neither client needs to
    know about the other, and neither needs to know this endpoint's
    internal bookkeeping at all.
    """
    # See the recursion_limit note in _resolve_run: this endpoint also
    # resumes a graph execution (ainvoke below), so it's subject to the
    # same GraphRecursionError risk on a long auto-approved loop.
    thread_id = _derive_thread_id(request.messages)
    _touch_thread(thread_id)
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": MAX_TOOL_ITERATIONS * 4 + 10,
    }
    snapshot = await agent_graph.aget_state(config)
    if not snapshot.next:
        raise HTTPException(status_code=409, detail="Aucune approbation en attente pour ce thread.")

    received_count = len(request.messages)
    stored_count = snapshot.values.get("owui_message_count") if snapshot.values else None
    if stored_count is not None and received_count == stored_count - 1:
        # No placeholder included: this client will append exactly one
        # new message after this call resolves (see docstring) —
        # anticipate that growth now.
        owui_message_count = received_count + 1
    else:
        # Placeholder already included (edited in place afterward, or an
        # unexpected count we fall back to trusting as-is): no further
        # growth expected.
        owui_message_count = received_count
    # Same plan-vs-tool distinction as in _resolve_run (Iteration 3, see
    # the comment there) — real bug found under real conditions during
    # the Iteration 3 live campaign: this endpoint used to
    # unconditionally update "approved"/"grant_session", leaving a
    # require_plan_approval pause blocked indefinitely (plan_approved
    # never set) since it was approved via /approve rather than via the
    # "approuver" text message.
    if "require_plan_approval" in snapshot.next:
        await agent_graph.aupdate_state(
            config,
            {
                "plan_approved": request.approved,
                "plan_grant_session": request.approved and request.grant_session,
                "owui_message_count": owui_message_count,
            },
        )
    else:
        await agent_graph.aupdate_state(
            config,
            {
                "approved": request.approved,
                "grant_session": request.approved and request.grant_session,
                "owui_message_count": owui_message_count,
            },
        )
    try:
        await agent_graph.ainvoke(None, config)
    except Exception:
        # Parity with _stream_response (streaming path): without this
        # safety net, an error here (e.g. LLM context overflow,
        # `llama-server`/TabbyAPI cutting the connection...) used to
        # surface as a raw 500 instead of a clean notice — observed under
        # real conditions during the tests_integration/test_web_tasks.py
        # harness (T8/T11, large real web pages). `_current_answer` is
        # NOT called here: the graph may have stopped mid-way with no
        # coherent state to re-read.
        logger.exception("Error during /approve (thread_id=%s)", thread_id)
        return {"content": _INTERNAL_ERROR_NOTICE}

    return {"content": await _current_answer(config)}


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    config, run_input = await _resolve_run(request)

    if request.stream:
        return StreamingResponse(
            _stream_response(config, run_input, request.model), media_type="text/event-stream"
        )

    try:
        await agent_graph.ainvoke(run_input, config)
    except Exception:
        # See the same note in /approve above: same safety net as the
        # streaming path (_stream_response), missing here until now.
        logger.exception(
            "Error during non-streaming /v1/chat/completions (thread_id=%s)",
            config["configurable"]["thread_id"],
        )
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": request.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": _INTERNAL_ERROR_NOTICE},
                    "finish_reason": "stop",
                }
            ],
        }

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": request.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": await _current_answer(config)},
                "finish_reason": "stop",
            }
        ],
    }
