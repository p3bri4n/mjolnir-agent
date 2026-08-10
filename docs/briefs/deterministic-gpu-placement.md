# Deterministic GPU placement — brief

> **Finding (2026-08-10)**: TabbyAPI loads with `Loading with autosplit` — no
> explicit `gpu_split` is configured. ExLlamaV3 therefore fills devices in the
> current CUDA order, and that order is not stable across restarts. Observed
> right now: **14 GB on the RTX 5060 Ti** (the slower card, 84 % utilisation)
> against 4.4 GB on the RTX 4070 Ti SUPER, which sits at 0 %.
>
> **Consequence**: an uncontrolled variable has been active across every
> campaign measured so far. Decode throughput depends on which card carries
> most of the layers, so part of the median-time variance between campaigns
> may come from placement rather than from the mechanisms under test. Scores
> are unaffected; latency comparisons are.
>
> **This ships before the next smoke.** One variable, its own measurement.

---

## 1 — Pin the device order

`docker-compose.yml`, `tabbyapi` service:

```yaml
    environment:
      - CUDA_DEVICE_ORDER=PCI_BUS_ID
```

Read at CUDA initialisation, so `docker compose up -d --force-recreate
tabbyapi` is required — a restart does not suffice.

With PCI ordering, index 0 is the RTX 5060 Ti (bus `04:00.0`) and index 1 the
RTX 4070 Ti SUPER (bus `08:00.0`). Stable, but it puts the **slower card
first** — which is why step 2 cannot be skipped.

Check whether `CUDA_VISIBLE_DEVICES` is set anywhere in the stack: PCI
ordering applies *before* it, so any index written there may change meaning.
Report before changing anything if it exists.

## 2 — Replace autosplit with an explicit split

In `services/tabbyapi/config.yml`: disable auto-split and set an explicit
split that puts most layers on the **RTX 4070 Ti SUPER**.

- Verify the exact parameter names against the installed TabbyAPI version
  (config models in the running image), not from memory — `gpu_split` and
  `gpu_split_auto` are the expected names, confirm them.
- Budget note: the 4070 Ti SUPER drives the display (`Disp.A = On`,
  ~67 MiB idle, more under desktop load). Leave headroom on that card.
- Total footprint today is ~18.5 GB across both cards (model + vision +
  MTP + cache). Aim for the largest share the Ada can take without risking
  OOM under campaign load, and state the chosen values with the reasoning.

## 3 — Verify against the real, not the config

After `--force-recreate`:

```
docker compose logs -f tabbyapi          # loading report, expect no "autosplit"
docker compose exec tabbyapi nvidia-smi --query-gpu=index,name,memory.used,pci.bus_id --format=csv
```

Both outputs go into the engineering log. The split is confirmed only when
`nvidia-smi` shows the intended distribution — not when the config file says
so.

## 4 — Measure the effect

Smoke on a fixed subset (same tasks, same flags, nothing else changed),
before and after. Judges: decode throughput (tokens/s), prefill time, median
time per task. A gain is expected; record it either way.

**If the gain is material, note explicitly in the log that median-time
figures from earlier campaigns are not comparable to later ones** — placement
was uncontrolled. Scores remain comparable.

## 5 — Add to the preflight

`campaign_preflight.py` gains a device-placement check: read the effective
per-GPU memory distribution and refuse the campaign if it deviates from the
expected split. Serialise device identity (name, index, bus id, memory used)
into campaign metadata alongside the behaviour flags — a campaign must be
able to say which hardware layout it measured.

This is the durable fix: pinning the config without checking it at campaign
start would let the same class of drift return silently.

## Out of scope

- Changing quantisation, context size, or any other TabbyAPI parameter.
- Re-running past campaigns.
