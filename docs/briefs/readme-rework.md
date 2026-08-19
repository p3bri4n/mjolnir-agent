# README rework for publication — brief

> **Goal**: make the README convincing to a technical visitor arriving from an
> article, in under thirty seconds, without turning it into a template. The
> project's differentiator is that it **measures what it claims** — the README
> must show that above the fold, not bury it under a feature list.
>
> **Anti-goal, stated first**: the generic structure (Features / Installation /
> Usage / Contributing / License, neutral marketing voice, three rows of
> badges) is now read as AI-generated filler by exactly the audience being
> targeted. The current README already avoids it — "Known, accepted
> limitations" is a section no template produces. Keep that voice.
>
> Target length: 800–1500 words. Anything longer moves to `docs/`.

---

## 1 — Above the fold (the only part most visitors read)

In order, before any scrolling:

1. **Title + one-sentence value proposition.** What it is, who it's for, and
   the constraint that makes it interesting. Current opening is close;
   tighten it so the hardware reality and the local-only stance are in the
   first sentence.
2. **Two or three real badges only** — CI (exists), license, and optionally
   the model/backend. No vanity rows.
3. **The demo GIF** (see §2).
4. **The numbers.** Three or four lines, no table needed: task suite size,
   latest campaign result, number of archived campaigns, hardware it runs
   on. This is the differentiator and it currently appears nowhere near the
   top.
5. **Quick start**, still above or immediately below the fold — the exact
   commands, copy-paste correct.

Everything else follows.

## 2 — The demo GIF

**Scripted, not hand-recorded.** A hand-made recording will need redoing at
every UI change and cannot be verified. Write `scripts/record-demo.sh` that:

- starts the stack and the fixture profile;
- runs one chosen scenario end-to-end;
- records the screen region (`ffmpeg` on the X display, or `asciinema` +
  `agg` if a terminal-only take is preferred);
- converts to an optimised GIF (`ffmpeg` palette pass, or `gifski`);
- writes to `docs/assets/demo.gif`.

**Scenario choice — this matters more than the production quality.** It must
show what no other project shows, in under 25 seconds:

- the agent receives a task and starts acting;
- it hits the URL-fabrication guardrail and **corrects itself** (this is the
  moment: a mechanical constraint visibly working);
- an approval prompt appears and is granted;
- the task completes with the asserted value.

Use a local fixture, never a real site: reproducible, no third-party content,
no risk of leaking anything. Keep it under ~4 MB or GitHub will make the page
crawl — trim frame rate before resolution.

Optional second asset, cheaper and useful: a **static screenshot of the
campaign dashboard** mid-run, showing per-task results. It says "this is
measured" in one image.

## 3 — Diagrams

Mermaid, rendered natively by GitHub, no plugin, no external service.

**Diagram 1 — architecture, five to seven nodes maximum.** Open WebUI →
langgraph-agent → mcp-client → (playwright-mcp, filesystem, terminal) →
TabbyAPI. Do not draw every service; the goal is ten-second comprehension,
not completeness. The exhaustive version stays in `docs/architecture/`.

**Diagram 2 — the approval tier flow**, if it stays legible: action → tier
classification → auto / session grant / individual approval / never
grantable. This is the project's most distinctive mechanism and it is
currently only prose.

Do not add a third. Two diagrams inform; four decorate.

## 4 — Restructure the body

- **Move `## Layout`** (the directory tree) to `docs/` and link it. It is
  reference material, not a selling point, and it costs a lot of vertical
  space.
- **Promote `## Known, accepted limitations`** — keep it, but consider
  moving it above `## Roadmap`. Honest limitations shown early buy more
  credibility than a roadmap.
- **Add `## Notes`** linking the two engineering articles (see the
  repository-gaps brief) and `docs/methodology.md` — that is where a
  curious reader goes next.
- **Requirements section, high up**: real minimum VRAM, what fits on a
  single card, what to reduce first (`cache_size`), and a "Tested on" line
  naming the exact configuration with the explicit note that other setups
  should work but are unverified.
- **Troubleshooting**, short: OOM at load and what to reduce; env vars read
  at import so `--force-recreate`; `entrypoint.sh` copied at build so
  rebuild; fixtures must be started before campaigns.
- **Support expectation**, one line: contributions welcome, no maintenance
  guarantee.

## 5 — Ship-readiness changes outside the README

Publishing brings visitors with different hardware. Before the first
article:

- `gpu_split_auto: true` as the shipped default; the explicit split becomes
  a documented option, with its reason (reproducible measurement).
- `.env.example` reviewed for permissive defaults.

## 6 — What not to do

- No animated banner, no emoji section icons, no "about me" paragraph.
- No last-updated date (it signals neglect the moment it ages).
- No badge that does not report a real fact.
- No feature table written in marketing voice — the current prose-with-
  headings style is better and rarer.
- Do not claim any capability that is not in the code. The security section
  already had to be corrected once for this.

## Judge

Give the reworked README to someone who has never seen the project (or a
fresh agent session) and ask three questions: what does it do, what do I
need to run it, and why should I trust the numbers. All three answerable
from the top half, in under a minute, or the rework is not done.
