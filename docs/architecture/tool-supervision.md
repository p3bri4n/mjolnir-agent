# Human supervision of tool calls

Content moved as-is from README.md (restructuring effort, see docs/briefs/restructuration-et-anglais.md, phase 3) — no rewrite at this stage.

Every tool call requested by the LLM (`terminal`, `filesystem`, `git`,
`browser`, `desktop`/GhostDesk) suspends the LangGraph graph instead of
running automatically (`require_approval` node,
`services/langgraph-agent/app/graph.py`). The agent then replies in the
conversation with a `⚠️ Approbation requise pour : ...` message offering
three replies: "approuver" (once), "approuver pour la session" (see
Session grants below), or "refuser" (a "Rejected by the user" error
`ToolMessage` is sent back to the LLM, which can react normally).

**Reversibility-tier policy** (`services/langgraph-agent/app/
approval_policy.py`), which replaces the old binary whitelist:

| Tier | Behavior | Default examples |
|---|---|---|
| `TIER_READ` (read) | auto, silent | `screen_shot`, `mouse_move`, `app_list`, `app_running`, `app_status`, filesystem/git reads (`read_file`, `git_status`, `git_log`...), `run_command` (mcp-terminal, already a strict read-only allowlist) |
| `TIER_REVERSIBLE` (reversible) | auto + logging (see Phase 2, audit log) | `mouse_click`, `mouse_double_click`, `mouse_drag`, `mouse_scroll`, `key_press`, `app_launch`, `clipboard_set`, confined filesystem/git writes (`write_file`, `git_commit`...) |
| `TIER_SENSITIVE` (sensitive) | human approval required | `key_type` (free-text input), everything else, **and any unknown tool** |

**`NEVER_GRANTABLE_TOOLS`** (Phase 1d-revised, see docs/history.md, T5):
`browser_run_code_unsafe` and `browser_evaluate` stay `TIER_SENSITIVE`
even when granted "for the session" — a grant normally relaxes a
sensitive tool to reversible for the rest of the thread, but running
arbitrary code in the page is an escalation, not a read primitive; every
call of these two tools requires individual approval, no exception.

**`browser_extract`** (Phase 1d-revised, see docs/history.md "extraction
fix"): observed under real conditions that making `browser_evaluate`
non-grantable made its usage disappear (T1/T10) with no replacement —
replaced by markedly less reliable manual exploration (ctrl+f, page-by-page
browsing). `browser_extract(query)` (synthetic tool,
`services/mcp-client/app/main.py`) provides the missing capability —
searching for text in the page and getting its context — via a FIXED JS
template (the query is interpolated via `json.dumps`, never concatenated
into executable code), `TIER_READ` tier: the model never supplies code,
so no escalation, unlike `browser_evaluate` which stays `NEVER_GRANTABLE`.

**Argument-based rules** (Phase 4, `RULES`/`_load_rules` in
`approval_policy.py`, `tool(pattern)` format à la Claude Code): refine a
tool's tier based on ITS ARGUMENTS rather than its name alone.
Implemented as named matchers in Python (no generic pattern DSL), not as
a simple AND with the static tier — a matching rule fully overrides
`tool_tier()`. Default rule: `key_type(len<50,no_newline)` →
`TIER_REVERSIBLE` (short, single-line input, harmless enough not to
warrant approval on every keystroke), whereas `key_type` stays
`TIER_SENSITIVE` by default for everything else (long or multi-line text
— pasted script, code...). A `command_prefix` matcher is also provided
(command prefixes, e.g. for `run_command` on the mcp-terminal side) but
with no default rule, since that server already only exposes a read-only
allowlist. In case of ambiguity (several named rules for the same tool
match at once), the most restrictive tier wins. `APPROVAL_RULES_PATH`
(env var, optional) points to a YAML file that supplements these default
rules (never replaces them) — see `_load_rules_from_yaml` for the exact
format (`tool`/`matcher`/`tier`, `command_prefix` additionally taking
`prefixes`).

The default is always the most restrictive tier, never the opposite: a
tool that appears in neither `TIER_READ_TOOLS` nor `TIER_REVERSIBLE_TOOLS`
(overridable via these CSV env vars) is automatically `TIER_SENSITIVE`.
Routing in `has_tool_calls`: a turn where **all** tool_calls are read or
reversible tier skips `require_approval`; a mixed turn (even a single
sensitive tool) stays fully subject to approval, for safety — no partial
per-tool approval.

`AUTO_APPROVED_TOOLS` (old env var) remains usable as a backward-compatible
override: any tool listed there is treated as `TIER_REVERSIBLE` even if
it's in neither list above. Empty by default now — the old historical
defaults (`app_list, app_running, screen_shot, mouse_move, mouse_click,
mouse_double_click, mouse_drag, mouse_scroll`) are already covered by the
default tiers above, so this new empty default reproduces the same
behavior for a deployment that doesn't set this variable.

One deliberate exclusion despite its misleading name: `clipboard_get`
stays `TIER_SENSITIVE` despite its "read" name — it can exfiltrate
sensitive data copied by the user (password, token...), no less
sensitive than `clipboard_set`.

`key_type`/`key_press` stay outside `TIER_READ`, but a **sequence** of
auto-approved `mouse_click` calls could in theory compose arbitrary input
via an on-screen virtual keyboard, effectively bypassing this exclusion —
see `AUTO_APPROVAL_STREAK_LIMIT` right below, which applies to any
auto-approved tool (read or reversible tier), not just the old
`AUTO_APPROVED_TOOLS` list.

**Virtual-keyboard guardrail** (`AUTO_APPROVAL_STREAK_LIMIT`, env var,
default `6`): beyond this many consecutive auto-approved turns *without a
human pass*, `has_tool_calls` forces the next turn back through
`require_approval` — even if it only contains normally auto-approved
tools. `auto_approval_streak` counter in `AgentState`, incremented on
every executed turn (`call_tools`) and reset to 0 as soon as a human
actually grants an approval (`require_approval`, only on resume, not
during the pause). Distinct from `tool_iterations`/`MAX_TOOL_ITERATIONS`,
which measures a total budget for the whole task rather than a number of
*consecutive turns without supervision*.

**Session grants** (Phase 3, `AgentState.session_grants` in
`app/graph.py`): replying "approuver pour la session" rather than
"approuver" adds the pending turn's tool(s) to a `session_grants` list
scoped to that thread. A tool listed there is then capped at
`TIER_REVERSIBLE` (auto + audit, see Phase 2 below) for the rest of the
conversation — `approval_policy.effective_tier()` accounts for this on
top of the tool's static tier. A grant never applies retroactively: the
turn that requests it stays subject to THIS approval, only *subsequent*
calls of the same tool benefit from it. Scope strictly per tool: granting
`key_type` does not exempt `browser_navigate`.

These grants live in the graph's state, hence in the same `MemorySaver`
checkpointer (in-memory only, see the Data persistence section) as the
rest of the thread — **they die with it**: a service restart loses them
exactly as it loses a pending approval, since there's no distinction
between "losing the thread's state" and "losing the grants it held".
Intended behavior for local use: no grant persistence across restarts,
every new conversation (or resumption after a restart) starts over with
no approval history.

**Audit log** (Phase 2, `services/langgraph-agent/app/audit_log.py`,
blind spot fixed — see docs/history.md, T9 investigation): every actually
executed tool_call whose tier isn't `TIER_READ` (silent by design,
nothing new to audit) is logged as JSONL under `AUDIT_LOG_DIR` (default
`/workspace/.audit`, same bind mount as the filesystem/git/terminal MCP
servers — see `docker-compose.yml`), one file per day
(`YYYY-MM-DD.jsonl`). Each line: `timestamp`, `thread_id`, `tool`,
`arguments`, `tier`, `result` (the tool's result EXACTLY AS SEEN BY THE
MODEL — already truncated/tiered if `browser_*`, never the raw version;
added in Phase 1d-revised, see docs/history.md, to reconstruct not just
the call sequence but also what the agent actually perceived at each
step). Volume-based rotation on top of the daily file: beyond
`AUDIT_LOG_MAX_BYTES` (default 20 MiB), the day's file is compressed
(`.N.jsonl.gz`) before the next write — `read_entries`/`GET /audit`
transparently read the compressed archives back.

**Before this fix**, only a tool_call arriving directly from
`has_tool_calls` (without going through `require_approval` this turn) was
audited — on the assumption that a turn that went through
`require_approval` already has a human in the loop, already traced in the
conversation history ("⚠️ Approbation requise" + the reply), so
duplicating it would be pointless. Assumption false in practice: in
automated campaigns, `_approve(..., grant_session=True)` (the test
harness) plays this role without any human ever watching, and the
conversation history itself doesn't survive a service restart
(`MemorySaver` checkpointer, in-memory only — see Data persistence
below): the audit log is then the ONLY persistent trace. The very first
call of each tool per thread — the most useful one for investigation —
therefore stayed invisible, even in campaigns. Now, any tool_call that
goes through `require_approval` gets audited too, with its real tier
(including `TIER_SENSITIVE`) — `GET /audit?thread_id=...` (optional,
returns the whole available log without it) allows consultation; an
individual corrupted line is skipped when reading rather than failing
the whole request.

**Assistant messages** (Phase 1d-revised, see docs/history.md
"OBSERVABILITY"): `call_llm` also logs EVERY turn of the model
(`audit_log.log_message`, `kind: "message"`, `role: "assistant"`,
`content: {content, tool_calls}`) — `<think>` reasoning and text
included, tool_calls if any — with no tier filtering, unlike the
tool_calls above: it's the agent's reasoning, not a side effect to be
selective about. Fills a gap that concretely blocked an archive diagnosis
(T1/T7/T10, see docs/history.md): before this addition, the archive only
allowed reconstructing the call sequence and their results, never what
the model itself had reasoned or answered at each step.

**Isolation between tasks** (Phase 1d-revised, see docs/history.md
"isolation between tasks"): `playwright-mcp` is a PERSISTENT MCP session
SHARED by all of mcp-client (not scoped per thread nor per task) — a tab
left open by one task stays visible in the snapshot of a completely
different, later task, potentially hours later. `POST
/reset-session/{server_name}` (mcp-client) drops the cached session (the
next call reopens a fresh one); the web-task harness calls it before
every repetition (see `tests_integration/test_web_tasks.py`,
`_reset_browser_session`).

Same problem, different channel (T9 investigation, see docs/history.md):
GhostDesk drives a real MACHINE-wide desktop (`app_launch`), with no
relation whatsoever to the Playwright session above nor to the current
thread — a window left open by one task stays readable (via
`screen_shot`) by a later task, hours after. `_reset_ghostdesk_desktop()`
(`pkill -f firefox` on the `ghostdesk` container) called before every
repetition, same guarantee as the Playwright reset.

**UI-button approval, without going through a text message**: two
endpoints complement the "approuver"/"approuver pour la session"/"refuser"
text flow —

- `POST /pending` (read-only, doesn't modify any state): reports whether
  the thread derived from `messages` is in an approval pause, and returns
  the request's text. Depends only on the first human message
  (`thread_id` derivation), never on the content of the last assistant
  message — the latter can be empty or truncated on the client side
  depending on how Open WebUI interprets `<think>` tags.
- `POST /approve` (`{"messages": [...], "approved": bool, "grant_session":
  bool}`): resumes the paused thread directly from an out-of-band
  decision (Open WebUI Action function), by editing the existing "⚠️
  Approbation requise" message in place rather than adding a new one —
  hence a `owui_message_count` bookkeeping without the `+1` applied to
  the normal text flow. `grant_session` (optional, default `false`,
  ignored if `approved=false`) mirrors "approve for the session" for this
  out-of-band flow. Returns 409 if there's no pending approval for this
  thread.

**Streaming fix**: when the model reasons (`<think>` tags) before
deciding on a tool call, the turn ends with empty actual `content` (the
tool_call travels over a separate channel), so no content chunk ever
closes the tag on the client side. Without a fix, the approval text that
follows would end up concatenated inside the still-open `<think>` —
invisible outside Open WebUI's collapsed thinking bubble.
`_stream_response` (`app/main.py`) now closes the tag before emitting
this text, based on what was actually streamed to the client (not on the
state already repaired internally by `call_llm`).

Since Open WebUI doesn't provide a stable conversation identifier to
`/v1/chat/completions` (it just resends the full history on every call),
the associated LangGraph thread is found by deriving a deterministic
`thread_id` from the hash of the conversation's first message
(`_derive_thread_id`, `services/langgraph-agent/app/main.py`). **Accepted
limitation**: two distinct conversations starting with a strictly
identical message would share the same thread — acceptable for local,
single-user use, not beyond that. A real fix would exist on the Open
WebUI side (writing a "Pipe function" that retrieves its internal
`chat_id` and forwards it upstream) but Open WebUI currently doesn't pass
this metadata to an external OpenAI-compatible backend like this one
(known limitation, documented by the project, unresolved to date:
[discussion #6999](https://github.com/open-webui/open-webui/discussions/6999)).

Since this thread now persists for the whole duration of a conversation
(not just during an approval pause), and Open WebUI resends the full
history on every turn on top of what's already persisted,
`owui_message_count` (a field in the graph's state) keeps track of how
many Open WebUI messages have already been ingested — only the new
message is then submitted on the next turn, which avoids duplicating the
history (a bug actually encountered and fixed during development, see
the table above).

No dependency version was changed to implement this feature:
`langgraph==0.2.34` (already pinned) already provided `NodeInterrupt`,
`MemorySaver` and the async `aget_state`/`aupdate_state` methods needed —
the fragile `langgraph`/`langchain-openai`/`openai` combination documented
above for streaming was therefore left untouched.

- **Embedding model download** (`sentence-transformers`): no test could be
  run with network access to `huggingface.co` in the development
  environment used. The Qdrant logic is covered with a deterministic fake
  embedder (see the Tests section), but `SentenceTransformer.encode()`
  under real conditions was not exercised.
- **Real Docker container spawning by `mcp-client`**: covered with a real
  MCP server launched as a direct Python process (same protocol as the
  real servers), but not with the Docker socket nor the real `mcp/*`
  images.
- **`llama-server`: build, startup and text inference actually verified**
  (`Qwen3.6-35B-A3B` model, `Q5_K_M` quant + `mmproj-F16`, a full
  end-to-end conversation through `langgraph-agent`, see the Inference
  backend section and the bug table). **Not verified: real function
  calling with an actual tool_call** (the integration tests covering
  `has_tool_calls`/`require_approval`/`call_tools` remain based on
  simulated LLM responses, see the Tests section) **and native WebP
  decoding under real conditions** (`IMAGE_FORMAT_PASSTHROUGH=webp` —
  tested only in plain-text conversation, never with a real GhostDesk
  `screen_shot`); no load testing either.
