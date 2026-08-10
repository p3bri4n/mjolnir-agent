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
