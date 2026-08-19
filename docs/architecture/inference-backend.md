# Inference backend

Content moved as-is from README.md (restructuring effort, see docs/briefs/restructuration-et-anglais.md, phase 3) — no rewrite at this stage.

The default backend is **TabbyAPI** (official image
[`ghcr.io/theroyallab/tabbyapi`](https://github.com/theroyallab/tabbyAPI),
ExLlamaV3 backend), serving **Qwen3.6-27B in EXL3 quantization** (VL
variant, vision preserved for `browser_take_screenshot`/the proactive OCR
capability — see Images and adaptive thinking and Proactive OCR
enrichment below), with **native MTP** (`draft_mode:
mtp` in `services/tabbyapi/config.yml`, the model's own multi-token
prediction head, no separate draft model to load).

Config `services/tabbyapi/config.yml` (mounted read-only): key fields
`model_dir`/`model_name` (HuggingFace-style directory of the EXL3 quant
under `./models`, **not** a `.gguf` — see below), `backend: exllamav3`,
`cache_mode`/`cache_size`/`max_seq_len` (to be tuned against the combined
VRAM available across the two GPUs), `draft_model.draft_mode: mtp`,
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
Qwen3.6 reasons by default on every turn (extended thinking tags), costly
in latency for a fast perception-action loop where each turn only has to
decide "where to click next". If enabled, `_apply_adaptive_thinking` adds
a transient `/no_think` system prompt (also never persisted in the
graph's state, same principle as the image retention above) when **all**
tool_calls of the previous turn were auto-approved (same per-tier policy
as `has_tool_calls`, session grants included — see `approval_policy.py`).
No injection on a task's very first turn (no previous tool_calls to
evaluate) nor as soon as a sensitive tool was involved in that previous
turn: full reasoning keeps its full value there.
