"""
Minimal homegrown Prometheus parser for llama-server's GET /metrics
(--metrics, see README). No dependency on the official prometheus_client
lib (parsing-side, unneeded here): only ~6 "llamacpp:*" metrics matter to
us, a line-by-line parser is largely enough.
"""

from typing import Optional

# Names exposed by llama-server ("llamacpp:<name>" convention, see
# --metrics in its README) mapped to stable dashboard-side keys,
# independent of the exact Prometheus name.
_METRIC_KEYS = {
    "decode_tokens_per_sec": "llamacpp:predicted_tokens_seconds",
    "prefill_tokens_per_sec": "llamacpp:prompt_tokens_seconds",
    "kv_cache_usage_ratio": "llamacpp:kv_cache_usage_ratio",
    "kv_cache_tokens": "llamacpp:kv_cache_tokens",
    "requests_processing": "llamacpp:requests_processing",
    "requests_deferred": "llamacpp:requests_deferred",
}


def parse_prometheus_text(text: str) -> dict[str, float]:
    """
    Extracts `metric_name -> value` from a text-format Prometheus
    exposition body. Ignores `# HELP`/`# TYPE` lines and labels
    (`metric_name{label="x"} value` -> key `metric_name`, labels
    discarded: llama-server doesn't put any on the metrics we care about
    here). An unreadable line (non-numeric value, unexpected format) is
    skipped rather than failing the whole parse.
    """
    metrics: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name_part, _, value_part = line.rpartition(" ")
        if not name_part:
            continue
        name = name_part.split("{", 1)[0]
        try:
            metrics[name] = float(value_part)
        except ValueError:
            continue
    return metrics


def extract_llama_metrics(text: str) -> dict[str, Optional[float]]:
    """Subset useful to the dashboard, `None` value if the metric is absent from the payload."""
    raw = parse_prometheus_text(text)
    return {key: raw.get(prom_name) for key, prom_name in _METRIC_KEYS.items()}


# Keys kept from a /slots slot (see llama-server README, --slots): the
# name of the field holding the number of context tokens already used by
# this slot differs by version (n_past historically, tokens_predicted /
# n_tokens on more recent builds) — the first one present is taken
# rather than depending on a single exact name.
_SLOT_USED_TOKEN_KEYS = ("n_past", "tokens_predicted", "n_tokens")


def normalize_slot(slot: dict) -> dict:
    used_tokens = next((slot[key] for key in _SLOT_USED_TOKEN_KEYS if key in slot), None)
    return {
        "id": slot.get("id"),
        "n_ctx": slot.get("n_ctx"),
        "is_processing": bool(slot.get("is_processing", False)),
        "used_tokens": used_tokens,
    }


def normalize_slots(slots: list) -> list[dict]:
    return [normalize_slot(s) for s in slots if isinstance(s, dict)]
