# mcp-client: single-caller assumption

`services/mcp-client/app/main.py` keeps one persistent MCP session per
*server name*, not per caller:

```python
_persistent_sessions: dict[str, tuple[AsyncExitStack, ClientSession]] = {}
```

`_get_persistent_session(server_name)` returns the cached session if one
is alive, opening a new one otherwise — there is no caller/thread/worker
dimension in the key. The two isolation resets that exist today
(`_reset_browser_session`, `_purge_downloads_volume`, both in
`services/langgraph-agent/tests_integration/test_web_tasks.py`) are
themselves global and called serially before each campaign repetition —
they assume a single active conversation at a time, which nothing in
`mcp-client` enforces or documents.

**Consequence, independent of campaign parallelism**: two simultaneous
Open WebUI conversations, or a campaign launched while someone is using
the agent interactively, share the same browser session/tab state and
the same downloads volume. This reproduces the exact contamination
patterns already fixed once for campaign repetitions —
`docs/resolved-bugs.md` #28/#29 (downloads volume), #30 (stale
Playwright tab/session) — except live, between unrelated real
conversations, not between scripted repetitions.

**Not yet a problem in practice**: today's usage is single-operator,
sequential. It becomes load-bearing the day either (a) campaign runs go
parallel (effort 1.3, `docs/briefs/update-plan.md`) or (b) more than one
person/session uses the agent concurrently.

**Preferred fix, when either trigger above materializes**: scope
`_persistent_sessions` (and the two reset endpoints) by a caller-
supplied `worker_id`/session key rather than by server name alone. This
was identified as the same underlying defect as
`_tools_schema_cache` in `services/langgraph-agent/app/graph.py`
(process-lifetime global, filled once — see `docs/resolved-bugs.md`
#31): module-level shared state with no caller dimension. Not fixed here
— recorded so it stops being rediscovered from scratch (see
`docs/history.md`, "EFFORT 1.3").
