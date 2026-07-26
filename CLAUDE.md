# Project-specific instructions

1. read `CLAUDE.md`, the README (short) and the brief for the ongoing
   effort at the start of a session; `docs/history.md`/`docs/resolved-bugs.md`
   are consulted via targeted search (grep on a keyword), never in full.
2. resolved bugs must be recorded in docs/resolved-bugs.md
3. progress history must be recorded in docs/history.md
4. always inform the user of the commands they need to type if a docker
   service needs restarting/rebuilding
5. one phase = one PR. One commit = one nature of change (never a move
   and a rewrite in the same commit — the diff becomes unreadable and the
   file history, this repo's central argument, is lost).
6. STOP 🧑 at checkpoints.
7. No opportunistic refactor outside scope — propose it at the checkpoint.
8. Any claim about a library's behavior is verified against the installed code.
9. README updated as you go, existing style.
10. Suggest obvious simplifications when it's opportune
11. Code/doc language ("restructuring + English" effort, see
    `docs/briefs/`): new content in English — docstrings, comments,
    non-exposed internal identifiers, README/CLAUDE.md/PLAN.md/docs.
    Stays in French: system prompts/directives sent to the model and
    benchmark task prompts (behavior, not documentation — translated in
    an isolated phase 6, never mixed with a refactor); already-written
    entries of `docs/history.md`/`docs/resolved-bugs.md` (dated
    archives); approval messages/user-facing notices (separate decision
    to come).

## Docstrings/comments contract

- one line by default; expanded docstring only if the behavior is
  non-obvious (side effect, invariant, error contract, WHY of this
  choice);
- never a paraphrase of the signature, never an Args/Returns/Raises
  section when names and types already suffice;
- the comment explains the WHY; code that requires explaining the WHAT
  should be rewritten instead;
- history does not live in the code ("fixed in iteration 3") → a
  one-line pointer to `docs/history.md`/`docs/resolved-bugs.md`, never
  the detail copied in;
- **accepted exception**: a block documenting a verified external
  constraint (library behavior, backend gotcha, reason for a flag)
  stays — it's hard-won knowledge. Cut the paraphrase, keep the
  justification.

## Markdown contract

No summary of what precedes, no "Conclusion"/"Key points" section in
technical docs, no table when three lines suffice. One document = one
function.


# Context

The stack now serves Qwen3.6-27B EXL3 via TabbyAPI/ExLlamaV3 (dual-GPU,
vision + MTP), the langgraph/langchain-openai/openai trio is migrated to
1.x/2.x, and an MCP Playwright server is wired in alongside GhostDesk.
Goal of this effort: move the agent from "executes approved actions" to
"accomplishes multi-step web tasks autonomously", without weakening the
existing security model (approval tiers, PromptGuard, egress firewall).


# Development plan

See `PLAN.md` — detailed plan by phase (0 to 4), amendments integrated.
