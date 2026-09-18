# Inference backend

Content moved as-is from README.md (restructuring effort, see docs/briefs/restructuration-et-anglais.md, phase 3) — no rewrite at this stage.

The default backend is **TabbyAPI** (official image
[`ghcr.io/theroyallab/tabbyapi`](https://github.com/theroyallab/tabbyAPI),
ExLlamaV3 backend, runtime triplet `exllamav3 1.5.0+cu128.torch2.9.0` /
`torch 2.9.0+cu128` / `tabbyAPI 0.0.1`), serving **Qwen3.8-27B in EXL3
quantization (4.50bpw)** (VL variant, vision preserved for
`browser_take_screenshot`/the proactive OCR capability — see Images and
adaptive thinking and Proactive OCR enrichment below), with **native
MTP** (`draft_mode: mtp` in `services/tabbyapi/config.yml`, the model's
own multi-token prediction head, no separate draft model to load; ×2.47
decode speedup measured warm, 61.8 T/s vs. 25.0 T/s without, 57%
acceptance — see `docs/engineering-log.md`, "Qwen3.8-27B evaluation,
Phase 2 CLOSED"). Adopted over the prior Qwen3.6-27B (3.50bpw) build
after the Phase 3 campaign comparison — no meaningful net score gain
(within documented run-to-run noise) and a real, uncontrolled latency
cost from the model's own default thinking effort, but adopted anyway;
full reasoning in `docs/engineering-log.md`, "Qwen3.8-27B evaluation,
Phase 3 CLOSED".

Config `services/tabbyapi/config.yml` (mounted read-only): key fields
`model_dir`/`model_name` (HuggingFace-style directory of the EXL3 quant
under `./models`, **not** a `.gguf` — see below), `backend: exllamav3`,
`cache_mode`/`cache_size`/`max_seq_len` (tuned against the combined VRAM
available across the two GPUs — `max_seq_len: 40960`/`cache_size: 81920`
since 2026-09-18, raised from 32768/65536 after a real
`context_length_exceeded` on an exhaustive-verification task; `cache_size`
deliberately kept at 2x `max_seq_len`, see the config file's own comment
and `docs/engineering-log.md`, "D1 context-overflow mitigation probe —
max_seq_len/cache_size"), `draft_model.draft_mode: mtp`,
`tool_format`, and three deliberate deviations from TabbyAPI's defaults:
`disable_auth: true` (internal `agent-net` network only, same trust model
as `llama-server`/Ollama), `vision: true` (disabled by default even when
the model has vision capabilities) and `reasoning: true` (disabled by
default in TabbyAPI, required to parse Qwen's `<think>` blocks).

Target model: HuggingFace-style files (safetensors + `config.json` +
tokenizer) expected under `./models/agent-llm/` (or `MODELS_HOST_PATH`) —
**never downloaded automatically**, just like `llama-server`. The name
`agent-llm` (rather than the actual name of the downloaded HuggingFace
repo) is required to match the hardcoded `model="agent-llm"` in
`ChatOpenAI` (`services/langgraph-agent/app/graph.py`) without touching
the code — same convention as the Ollama aliasing below
(`scripts/rebuild-agent-llm.sh`).

### GPU split

`gpu_split_auto: true` (`services/tabbyapi/config.yml`) is the shipped
default — it works regardless of how many GPUs are installed, their VRAM,
or their bus order. A manual `gpu_split: [GB, GB, ...]` is a per-model,
per-machine value, not a global setting: the right split depends on which
quant is loaded (VRAM footprint changes with it) and on the exact cards
present, so it does not belong in the versioned config.

**Setting your own**: put it in a local, untracked override instead of
editing `config.yml` — `docker compose` auto-merges
`docker-compose.override.yml` (gitignored) without any `-f` flag:

```yaml
# docker-compose.override.yml
services:
  tabbyapi:
    volumes:
      - ./services/tabbyapi/config.local.yml:/app/config.yml:ro
```

`services/tabbyapi/config.local.yml` (also gitignored) is then a full copy
of `config.yml` with your own `gpu_split_auto: false` / `gpu_split: [...]`
— indexed by device index, so `CUDA_DEVICE_ORDER=PCI_BUS_ID`
(`docker-compose.yml`, service `tabbyapi`) must stay set for the index
order to be stable across restarts. `docker compose up -d --force-recreate
tabbyapi` after any change (config is read at container start).

**Why bother pinning it at all**: reproducible measurement. Whole-layer
splitting means memory-per-card only settles once the loader has run, so
comparing latency across campaigns needs the split held constant, not just
"whatever autosplit decides today" (see
`docs/briefs/archives/deterministic-gpu-placement.md`). The campaign
preflight enforces this automatically: `check_device_placement`
(`tests_integration/campaign_preflight.py`) compares the actual per-GPU
memory used against `EXPECTED_GPU_DEVICES`, a value kept in sync with
whatever split is configured — a campaign run under a silently different
split (autosplit left on, config drifted, wrong card at a given index) is
refused before the first task starts, rather than producing numbers that
look comparable and aren't.

**Current production value**: `gpu_split: [10, 13]` (Qwen3.8-27B
4.50bpw, `services/tabbyapi/config.local.yml`) — hand-picked after
autosplit put ~91% of GPU 0's capacity in use (14 787/16 311 MiB, only
~1.5 GiB free) vs. ~45% on GPU 1, too thin a margin for a
multi-repetition campaign. Verified stable across 3 reloads: GPU 0
landed on the exact same 10 991 MiB every time, GPU 1 within 102 MiB
(noise) — ~5.3-5.4 GiB free on each card, comfortably above the
~1.5-2 GiB target reasoned from this project's own prior incident (a
documented ~822 MiB margin flagged as insufficient for the vision
tower). Superseded the prior Qwen3.6 (3.50bpw) build's `[5, 14]`. Full
detail: `docs/engineering-log.md`, "Qwen3.8-27B evaluation — explicit
gpu_split adopted".


## Images and adaptive thinking (`services/langgraph-agent/app/graph.py`)

**Image conversion** (`IMAGE_FORMAT_PASSTHROUGH`, env var, default absent
= PNG conversion): `_to_png_data_uri` remains the default path — every
tool image result (e.g. `browser_take_screenshot`, native WebP) is
systematically re-encoded to PNG before being sent to the LLM. This is
the default for the TabbyAPI backend (ExLlamaV3 is not known to decode
WebP natively — to be verified empirically, see Inference backend above)
as it is for Ollama (mtmd decoder, explicit failure on WebP).

**Image retention** (`MAX_IMAGES_IN_CONTEXT`, env var, default `1`): only
the last `MAX_IMAGES_IN_CONTEXT` screenshots stay as multimodal
`image_url` blocks in the history submitted to the LLM on each call;
earlier ones are replaced by the placeholder text
`[screenshot antérieure supprimée]` (`_apply_image_retention`). **Never
touches the checkpointer**: this filtering only applies to the message
list built right before `bound_llm.astream()`, never to
`state["messages"]` itself — the full history, with all original images,
stays intact and replayable (e.g. if `MAX_IMAGES_IN_CONTEXT` changes from
one conversation to another). Motivation: a repeated screenshot loop
(e.g. `browser_take_screenshot`) can accumulate many captures in the history, each
costly in visual tokens, for near-zero value beyond the most recent one
(the only one reflecting the screen's current state).

**Adaptive thinking** (`ADAPTIVE_THINKING`, env var, default `false`):
Qwen models reason by default on every turn (extended thinking), costly
in latency for a fast perception-action loop where each turn only has to
decide "where to click next". If enabled, `_should_suppress_thinking`
disables reasoning for that one request
(`bound_llm.bind(extra_body={"enable_thinking": False})`, a real
per-request TabbyAPI/ExLlamaV3 parameter — never a prompt-level
injection) when **all** tool_calls of the previous turn were
auto-approved (same per-tier policy as `has_tool_calls`, session grants
included — see `approval_policy.py`). Thinking stays on for a task's very
first turn (no previous tool_calls to evaluate) or as soon as a sensitive
tool was involved in that previous turn: full reasoning keeps its full
value there. Migrated off an earlier `/no_think` text-prefix mechanism,
confirmed to have no effect on this backend — see
`docs/briefs/archives/qwen3.8-27b-evaluation.md`, Phase 1. **Not adopted
as a default on Qwen3.8**: a full-campaign measurement showed a real
-21% cumulative time gain, but also a real, mechanistically-confirmed
score regression on long-horizon multi-turn tasks (full suppression
removes the model's ability to notice a dead end and self-correct — see
`docs/engineering-log.md`, "ADAPTIVE_THINKING=true campaign").

**Reasoning effort** (`REASONING_EFFORT`, env var, **default `medium`**
since 2026-09-18): an independent mechanism from `ADAPTIVE_THINKING`
above — caps HOW DEEP reasoning goes (`extra_body={"reasoning_effort":
"xhigh" | "medium" | "low"}`, same bare-top-level-key wire convention as
`enable_thinking`, confirmed empirically rather than assumed from the
model's own Python example — see `docs/engineering-log.md`,
"reasoning_effort tuning, Phase 0") rather than suppressing it entirely.
Applied unconditionally on every `call_llm` invocation when set — no
turn-based gate. Built as a candidate fix for the reliability regression
above: keeps some deliberation on every turn instead of an all-or-
nothing cut, and adopted after a decisive measurement (60/62 vs.
`xhigh`'s own 57/62, -7.9% cumulative time, zero new `boucle` failures)
— see `docs/briefs/archives/reasoning-effort-tuning.md` for the full
history including the D1 confirmation that closed the one open wrinkle.
