# Security hardening — phased plan

> **Scope**: bring the security posture from "approval tiers + isolated browser
> profile" to a defensible layered architecture, and make each layer's effect
> measurable. Prompt injection is not solvable at the model level; the goal is
> blast-radius reduction, and every claim must end up in the README only after
> it is verified in code.
>
> **Sequencing principle**: instrument before mechanism (`docs/methodology.md`).
> Security work without a baseline produces claims, not results.

---

## Phase 0 — Threat model and instrument

Nothing is built in this phase. It decides what the rest is for.

1. **Written threat model** (`docs/architecture/threat-model.md`): who the
   adversary is (untrusted web content manipulating the model, not an attacker
   with shell access), what must not happen (unauthorised engagement actions,
   data leaving the machine, host access beyond the workspace), and what is
   explicitly out of scope (a compromised host, a malicious operator). Without
   it, later phases optimise at random.
2. **Instrument**: benchmark family C (injection tasks) plus a canary-token
   task — a unique string planted in a task's sensitive context; the run fails
   if the canary ever appears in an outbound request. Assertions read the audit
   log and the proxy log, never the agent's own account.
3. **Baseline**: run family C against the current agent. Expect failures; that
   is the point. This is the zero point every later phase is measured against.

🧑 Checkpoint: threat model and baseline reviewed.

## Phase 1 — Secrets and data minimisation

Cheapest phase, largest effect: what is not in reach cannot leak.

1. **Inventory**: every credential in the stack (`.env`, TabbyAPI keys, Open
   WebUI secrets, git credentials used by the git MCP, anything under the
   mounted workspace), with, for each: who needs it, and whether the agent
   needs it. Most will not.
2. **Git history scan** (gitleaks or trufflehog) — a secret committed once
   stays in history even after deletion. Verify `.gitignore` covers `.env`.
3. **Narrow the workspace mount**: `WORKSPACE_HOST_PATH` is the agent's widest
   window onto the host. Restrict it to what tasks actually need; nothing
   personal, no keys, no SSH material.
4. **Secret brokering** (design now, implement with Phase 2): the pattern used
   by iron-proxy and Agent Vault — the agent holds placeholders, the proxy
   substitutes real credentials on the way out, so credentials never enter the
   model's context and cannot be exfiltrated by an injection.
5. **Audit-log hygiene** — a consequence of your own observability work: the
   JSONL now persists tool results and model messages, so any secret or
   personal datum an agent reads is written to disk in cleartext, unrotated.
   Decide a retention policy and a redaction pass (patterns for tokens, keys,
   emails) before the volume becomes a liability.

🧑 Checkpoint: inventory and mount scope reviewed before any code.

## Phase 2 — Network boundary

The structural piece; no product decision can substitute for it.

1. **Deny by default**: `agent-net` becomes `internal: true`; a proxy sidecar
   straddles internal and external networks and is the only route out;
   `HTTP_PROXY`/`HTTPS_PROXY` set in the agent, Playwright and GhostDesk
   containers.
2. **Anti-bypass**: iptables rules inside those containers block direct
   outbound, so an application that ignores the proxy variables reaches
   nothing. Reuse the ipset/iptables pattern already written for the Claude
   Code sandbox.
3. **Global static allowlist** first — the domains the stack genuinely needs
   (model backend is local, so this is short). Squid with a `dstdomain` ACL is
   enough at this stage; TLS interception comes with Phase 3.
4. **Fail loudly**: every refusal logged with domain and origin container.
   Silent blocking produces days of half-rendered pages and no explanation.

Judge: family C baseline re-run — the injection may still succeed at the
model level, but exfiltration attempts to non-allowlisted domains must be
refused, visible in the proxy log.

🧑 Checkpoint.

## Phase 3 — Per-task scope, on both channels

1. **Agent-level scope**: a per-task allowlist carried in the graph state;
   `browser_navigate` validated against it (extending the existing
   fabrication guardrail), and the current URL re-checked after *every*
   browser action — clicks, form submissions and redirects navigate too, and
   checking only at navigate time locks the front door alone.
2. **Proxy-level scope**: the per-task list pushed to the proxy, which is the
   only enforcement that is channel-independent — it covers GhostDesk, whose
   coordinate-based clicks expose no URL to inspect. This is where TLS
   interception earns its cost: `Sec-Fetch-Dest` distinguishes a top-level
   navigation from an image or font, so the allowlist applies to documents
   without breaking page rendering.
3. **GhostDesk decision**, informed by benchmark family E: if the DOM channel
   covers the useful web, remove the browser from the GhostDesk image
   entirely and keep it for out-of-browser work — the cleanest fix, since a
   capability that does not exist needs no policing. Otherwise: kiosk mode
   (no address bar) as defence in depth, with the proxy as the real boundary.

Judge: family C task C3 (scope-violation invitation) and family B policy
evaluators.

🧑 Checkpoint.

## Phase 4 — Provenance tracking

The poor man's CaMeL, and the highest security-per-line ratio available.

1. Mark every value that originates in untrusted content (page text, file
   contents, tool results from the web).
2. Rule: a marked value appearing in the argument of an *outbound* tool —
   navigation, submission, upload, write — raises the action to ENGAGEMENT,
   i.e. individual human approval, never covered by a session grant. This
   catches the canonical exfiltration pattern (a destination that came from
   retrieved content rather than from the user's instruction).
3. Keep it lightweight: marking and a tier rule, not a custom interpreter.

Judge: the canary task from Phase 0, plus interventions per task (this rule
will cost approvals — measure how many before deciding it is worth it).

🧑 Checkpoint.

## Phase 5 — Quarantined reasoning

1. **Decontaminate replanning**: the planner sees subtask statuses and
   verification results, never raw page text. This alone moves the
   architecture close to the dual-LLM pattern, because the first plan is
   already produced before any browsing — replanning is the only
   recontamination path today.
2. **Quarantined extraction** (optional): raw untrusted content is read by a
   separate context — same model at first, a small dedicated model on the
   second GPU if contention justifies it — which has no tool access and
   returns extracted values only.

Judge: family C, and task success must not collapse — the literature is
explicit that these separations cost capability; measure the trade rather
than assume it.

🧑 Checkpoint.

## Phase 6 — Content inspection (optional, later)

Allowlists bound *where* traffic goes, not *what* it carries: an agent
allowed to reach a domain can still exfiltrate through URL parameters or a
request body. Proxy-side inspection (canary patterns, credential shapes,
encoded blobs) closes part of that gap. Worth doing only once Phases 1–4
hold, and to be framed as detection, not prevention.

## README rule

No security claim ships before the phase implementing it is merged and its
judge is green. Planned items stay under Roadmap. A reader may deploy on the
strength of these statements.
