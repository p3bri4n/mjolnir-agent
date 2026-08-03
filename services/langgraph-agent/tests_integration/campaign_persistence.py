"""
Persistance de campagne (constat de l'inventaire de persistance avant ce
chantier, voir docs/history.md "INVENTAIRE DE PERSISTANCE") : un fichier
`campaign-<timestamp>-<label>.json` par campagne, écrit UNE SEULE FOIS à la
fin (jamais réécrit ensuite), à côté du rapport Markdown existant
(`_write_report`, test_web_tasks.py, devient une VUE sur ce JSON plutôt
qu'une source de vérité propre — voir sa docstring). Corrige les 4 lacunes
du constat : (1) aucune ligne par run n'existait hors du Markdown prose,
(2) le thread_id — clé de jointure avec /workspace/.audit — n'était
consigné nulle part, (3) seuls des agrégats de métriques TabbyAPI
survivaient, (4) rien ne fixait la config effective (commit, digests
d'image, modèle, flags) au moment du run.

Fichier complémentaire (docs/briefs/B2-campaign-control.md, Part 1.1) :
`<campaign-id>.progress.json`, réécrit à CHAQUE frontière de run (pas une
fois à la fin) — voir write_progress_json/init_progress_state plus bas.
Sert la vue live (dashboard) et la reprise après pause, pas un remplacement
du fichier ci-dessus.

Correction rétroactive (B2 Part 2, pause/reprise) : le paragraphe ci-dessus
("écrit UNE SEULE FOIS à la fin") demandait DÉJÀ que ce fichier COMPLET
devienne lui aussi incrémental ("append as it goes... A campaign killed
mid-flight then keeps everything up to the last completed run") — annoncé
en Part 1.1 mais seulement fait pour le progress.json léger lors de la
première passe. Fait maintenant (append_campaign_row) : une pause perdrait
sinon les champs riches par run (texte final, échantillons TabbyAPI...) qui
ne vivaient qu'en mémoire dans _run_campaign(). write_campaign_json devient
une écriture atomique (temp+rename) en conséquence : appelée bien plus
souvent qu'"une fois à la fin" maintenant.

Correction factuelle avant implémentation (CLAUDE.md #8 — toute affirmation
sur le comportement d'une lib se vérifie contre le code installé) :
TabbyAPI (image `mjolnir-agent-tabbyapi`, vérifié dans
/app/endpoints/*/router.py de l'image réellement construite) N'EXPOSE PAS
d'endpoint /metrics Prometheus, contrairement à llama-server (voir
services/dashboard/app/prometheus.py et le commentaire déjà présent dans
docker-compose.yml, service dashboard : "Pas d'équivalent /metrics/{slots}
pour TabbyAPI à ce jour"). Il n'y a donc rien à "relever avant/après" sur un
endpoint qui n'existe pas. La seule source réelle de performance par
requête reste le texte des logs du conteneur (regex sur "N tokens generated
in ... Process: X cached tokens and Y new tokens at Z T/s", reprise ici de
l'ancien `_fetch_tabbyapi_prefill_stats` de test_web_tasks.py, qui
n'agrégeait QUE la somme/moyenne — voir aggregate_prefill_stats plus bas
pour l'équivalent agrégé, désormais dérivé des échantillons plutôt que
recalculé séparément) : `collect_tabbyapi_raw_samples` ci-dessous persiste
un échantillon PAR REQUÊTE journalisée dans la fenêtre du run, ce qui rend
un delta a posteriori calculable — l'intention de la demande — sans
prétendre à un endpoint fictif.
"""

import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

AGENT_CONTAINER = "langgraph-agent"
TABBYAPI_CONTAINER = "tabbyapi"

# Union de tous les os.environ.get(...) trouvés dans services/langgraph-agent/
# app/*.py (voir grep ayant servi à établir cette liste) : les flags qui
# pilotent réellement le comportement de l'agent, à l'exclusion des URLs
# internes de service (LLM_BASE_URL, MCP_CLIENT_URL...) qui sont fixes par
# construction du réseau docker-compose et n'apportent rien à un diagnostic
# de campagne.
CAMPAIGN_ENV_FLAGS = [
    "MAX_TOOL_ITERATIONS",
    "LLM_MAX_TOKENS",
    "PLANNER_ENABLED",
    "PLANNER_MAX_TOKENS",
    "PLANNER_THINKING_ENABLED",
    "VERIFICATION_ENABLED",
    "SUBTASK_ATTEMPT_BUDGET",
    "REPLAN_BUDGET",
    "PLAN_VALIDATION_ENABLED",
    "PLAN_JUDGE_ENABLED",
    "ADAPTIVE_THINKING",
    "MAX_IMAGES_IN_CONTEXT",
    "IMAGE_FORMAT_PASSTHROUGH",
    "IMAGE_TOKEN_ESTIMATE",
    "AUTO_APPROVAL_STREAK_LIMIT",
    "AUTO_APPROVED_TOOLS",
    "APPROVAL_RULES_PATH",
    "NEVER_GRANTABLE_TOOLS_EXTRA",
    "BROWSER_TOOL_OUTPUT_MAX_CHARS",
    "AFFORDANCE_THRESHOLD",
    "FABRICATION_LIMIT",
    "BROWSER_NAVIGATE_GUARDRAIL",
    "MAX_EMPTY_ANSWER_RETRIES",
    "AUDIT_LOG_MAX_BYTES",
    "EPISODE_COMPACTION_ENABLED",
    "EPISODE_COMPACTION_TURN_THRESHOLD",
    "TZ",
]

# Conteneurs dont l'image effectivement tournante fait partie de la config
# d'un run (README, "Arborescence") : les 3 services applicatifs propres à
# ce dépôt + le backend d'inférence. playwright-mcp est une image officielle
# non reconstruite par ce dépôt (voir README) : son ID d'image est quand
# même capturé (utile pour détecter un `:latest` qui a bougé), juste jamais
# "construit localement" au sens preflight.
CAMPAIGN_IMAGE_CONTAINERS = [
    "langgraph-agent",
    "mcp-client",
    "tabbyapi",
    "playwright-mcp",
]


def _run(args: list, timeout: int = 15) -> Optional[str]:
    """Best-effort : une métadonnée de contexte manquante ne doit jamais faire
    échouer une campagne entière (voir _fetch_tabbyapi_prefill_stats,
    test_web_tasks.py, même philosophie)."""
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def git_commit(repo_dir: Optional[Path] = None) -> Optional[str]:
    args = ["git", "rev-parse", "HEAD"]
    if repo_dir is not None:
        args = ["git", "-C", str(repo_dir), "rev-parse", "HEAD"]
    return _run(args)


def docker_image_id(container: str) -> Optional[str]:
    """ID de l'image RÉELLEMENT utilisée par le conteneur qui tourne (même
    primitive que campaign_preflight._fetch_tabbyapi_image_ids) — None si le
    conteneur n'existe pas/ne tourne pas, jamais une exception."""
    return _run(["docker", "inspect", "--format", "{{.Image}}", container])


def collect_image_digests(containers: list = None) -> dict:
    containers = containers if containers is not None else CAMPAIGN_IMAGE_CONTAINERS
    return {name: docker_image_id(name) for name in containers}


def fetch_tabbyapi_model_id(container: str = TABBYAPI_CONTAINER) -> Optional[str]:
    """`id` du ModelCard renvoyé par GET /v1/model (voir
    /app/endpoints/core/router.py de l'image tabbyapi, "Currently loaded
    model endpoint") — vérité terrain de ce qui est RÉELLEMENT chargé,
    plutôt que de relire config.yml (qui ne garantit pas que le rechargement
    a eu lieu). disable_auth: true (config.yml) : pas de clé requise."""
    script = """
import json, urllib.request
with urllib.request.urlopen('http://localhost:5000/v1/model', timeout=10) as r:
    print(json.loads(r.read().decode()).get('id'))
"""
    out = _run(["docker", "exec", "-i", container, "python3", "-c", script], timeout=15)
    return out or None


def collect_env_flags(container: str = AGENT_CONTAINER, flags: list = None) -> dict:
    """Flags D'ENV TELS QUE VUS PAR LE CONTENEUR qui tourne (pas le process
    hôte qui lance pytest, qui peut ne pas les avoir/en avoir des périmés) —
    `docker exec env`, filtré à CAMPAIGN_ENV_FLAGS. Absent du conteneur =
    absent du dict (pas de valeur par défaut inventée ici : app/*.py a déjà
    ses propres défauts, les dupliquer ici périmerait silencieusement à leur
    moindre changement)."""
    flags = flags if flags is not None else CAMPAIGN_ENV_FLAGS
    out = _run(["docker", "exec", container, "env"])
    if out is None:
        return {}
    found = {}
    for line in out.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key in flags:
            found[key] = value
    return found


def collect_metadata(label: str, repo_dir: Optional[Path] = None) -> dict:
    return {
        "commit": git_commit(repo_dir),
        "image_ids": collect_image_digests(),
        "tabbyapi_model_id": fetch_tabbyapi_model_id(),
        "env_flags": collect_env_flags(),
        "label": label,
    }


_TABBY_METRICS_RE = re.compile(
    r"(\d+) tokens generated in ([\d.]+) seconds \(Queue: ([\d.]+) s, Process: (\d+) cached tokens "
    r"and (\d+) new tokens at ([\d.]+) T/s"
)


def collect_tabbyapi_raw_samples(since_dt: datetime, until_dt: datetime, container: str = TABBYAPI_CONTAINER) -> list:
    """Un échantillon PAR REQUÊTE TabbyAPI journalisée dans la fenêtre
    [since_dt, until_dt] (voir docstring du module pour pourquoi ce n'est
    pas un relevé /metrics). Best-effort, jamais d'exception : renvoie []
    si `docker logs` échoue (conteneur redémarré/arrêté entre-temps)."""
    since_iso = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    until_iso = until_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        result = subprocess.run(
            ["docker", "logs", "--since", since_iso, "--until", until_iso, container],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    text = (result.stdout or "") + (result.stderr or "")
    normalized = re.sub(r"\s+", " ", text)
    samples = []
    for m in _TABBY_METRICS_RE.finditer(normalized):
        samples.append(
            {
                "tokens_generated": int(m.group(1)),
                "generation_seconds": float(m.group(2)),
                "queue_seconds": float(m.group(3)),
                "cached_tokens": int(m.group(4)),
                "new_tokens": int(m.group(5)),
                "process_speed_tps": float(m.group(6)),
            }
        )
    return samples


def aggregate_prefill_stats(samples: list) -> dict:
    """Même calcul que l'ancien _fetch_tabbyapi_prefill_stats
    (test_web_tasks.py, avant ce chantier) mais depuis les échantillons déjà
    collectés par collect_tabbyapi_raw_samples — évite un second `docker
    logs` sur la même fenêtre temporelle pour le même résultat.

    prompt_tokens_total (PLAN.md Phase 2, point 3 — trouvé manquant lors de
    la requalification de la campagne 2026-07-28_campaign_episode-
    compaction-enabled.md) : somme de cached_tokens+new_tokens, soit la
    taille RÉELLE du contexte envoyé à TabbyAPI pour chaque appel — un
    vrai juge tokens/tâche, distinct de prefill_seconds qui mélange volume
    de tokens ET taux de cache ET débit du backend (deux runs au même
    volume de tokens peuvent avoir des prefill_seconds très différents
    selon le cache hit rate, l'inverse aussi)."""
    prefill_seconds = 0.0
    cache_zero = 0
    prompt_tokens_total = 0
    for s in samples:
        if s["cached_tokens"] == 0:
            cache_zero += 1
        if s["process_speed_tps"] > 0:
            prefill_seconds += s["new_tokens"] / s["process_speed_tps"]
        prompt_tokens_total += s["cached_tokens"] + s["new_tokens"]
    return {
        "prefill_seconds": round(prefill_seconds, 2),
        "cache_zero_requests": cache_zero,
        "tabbyapi_requests": len(samples),
        "prompt_tokens_total": prompt_tokens_total,
    }


def campaign_id(label: str, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> str:
    timestamp = now().strftime("%Y%m%dT%H%M%SZ")
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "campagne"
    return f"{timestamp}-{slug}"


def campaign_json_path(directory: Path, cid: str) -> Path:
    return directory / f"campaign-{cid}.json"


def write_campaign_json(path: Path, metadata: dict, started_at: str, ended_at: str, rows: list) -> None:
    """Now called at every run boundary (see append_campaign_row below),
    not just once at campaign end (see module docstring, "correction
    rétroactive") — atomic (temp+rename) accordingly, same pattern as
    write_progress_json."""
    payload = {
        "metadata": {**metadata, "started_at": started_at, "ended_at": ended_at},
        "runs": rows,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def append_campaign_row(path: Path, metadata: dict, started_at: str, row: dict) -> None:
    """Incremental counterpart to write_campaign_json — reads the file if
    it already exists (earlier runs in THIS campaign, possibly from a
    prior segment before a pause), appends `row`, rewrites atomically.
    `ended_at` is set to now() on every call — only the LAST call's value
    survives, which is exactly the true end-of-campaign timestamp once the
    loop finishes."""
    existing_rows = read_campaign_json(path)["runs"] if path.exists() else []
    existing_rows.append(row)
    write_campaign_json(path, metadata, started_at, datetime.now(timezone.utc).isoformat(), existing_rows)


def read_campaign_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def config_digest(metadata: dict) -> str:
    """Stable hash of the config a resume must not have drifted from (B2
    Part 3.3, docs/briefs/B2-campaign-control.md) — commit + image ids +
    env flags, excluding `label` (a resume relabels nothing) and the
    TabbyAPI model id (already covered by its image id)."""
    payload = json.dumps(
        {
            "commit": metadata.get("commit"),
            "image_ids": metadata.get("image_ids"),
            "env_flags": metadata.get("env_flags"),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def progress_json_path(directory: Path, cid: str) -> Path:
    return directory / f"{cid}.progress.json"


def pause_sentinel_path(directory: Path, cid: str) -> Path:
    """B2 Part 2.1: a bare file, presence-only — created by
    `run-campaign.sh --pause`, consumed (deleted) by the harness once it
    has acted on it."""
    return directory / f"{cid}.pause"


def init_progress_state(cid: str, label: str, started_at: str, digest: str, planned: list) -> dict:
    """Shape frozen by B2 Part 1.1 — `current`/`completed` are mutated by
    the caller (test_web_tasks.py's run loop, the only place that knows a
    run's boundaries) and persisted via write_progress_json() below.

    `planned` (extension beyond the brief's literal field list, needed to
    make it usable): the ordered list of `{task_id, repetition}` for every
    run in execution order — `total_runs` alone can't tell
    compute_remaining_eta() WHICH task each remaining run is (Part 1.4),
    and a resume (Part 2.3) needs the repetition number too, not
    reconstructible from a bare task_id list once some runs are already
    completed. `planned[len(completed):]` is the remaining-runs sequence.

    `segments` (Part 3.1, extension for the same reason): segment 0 is
    opened here even for a campaign that's never paused — the
    non-regression requirement ("a campaign run without pausing produces a
    report identical in shape... single segment") needs a segment to
    exist from the start, not only appear once a pause happens."""
    return {
        "campaign_id": cid,
        "label": label,
        "started_at": started_at,
        "total_runs": len(planned),
        "config_digest": digest,
        "planned": planned,
        "segments": [{"index": 0, "started_at": started_at, "ended_at": None}],
        "current": None,
        "completed": [],
        "paused": False,
    }


def open_new_segment(state: dict, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> int:
    """B2 Part 3.1 — called on resume, after the drift/staleness checks
    passed. Returns the new segment's index, used to tag every
    `completed` entry until the next pause (or campaign end)."""
    segments = state.setdefault("segments", [])
    index = len(segments)
    segments.append({"index": index, "started_at": now().isoformat(), "ended_at": None})
    return index


def close_current_segment(state: dict, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
    """Stamps the LAST segment's `ended_at` — called both on pause (Part
    2.1) and at true campaign completion, so every segment (including the
    only one in a never-paused campaign) ends up with a closed window."""
    segments = state.get("segments")
    if segments:
        segments[-1]["ended_at"] = now().isoformat()


def config_drift_diff(recorded_metadata: dict, current_metadata: dict) -> Optional[str]:
    """B2 Part 3.3 — None if no drift, else a human-readable diff of
    exactly what changed (commit / image ids / env flags, the same fields
    config_digest() hashes) since the campaign started. A resume whose
    second half measured a different agent is void; a bare digest mismatch
    wouldn't say WHY to refuse."""
    diffs = []
    if recorded_metadata.get("commit") != current_metadata.get("commit"):
        diffs.append(f"commit: {recorded_metadata.get('commit')} -> {current_metadata.get('commit')}")
    for name, recorded in (recorded_metadata.get("image_ids") or {}).items():
        current = (current_metadata.get("image_ids") or {}).get(name)
        if recorded != current:
            diffs.append(f"image[{name}]: {recorded} -> {current}")
    for flag, recorded in (recorded_metadata.get("env_flags") or {}).items():
        current = (current_metadata.get("env_flags") or {}).get(flag)
        if recorded != current:
            diffs.append(f"flag[{flag}]: {recorded} -> {current}")
    return "; ".join(diffs) if diffs else None


def check_resume_staleness(
    state: dict, max_days: int = 7, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
) -> Optional[str]:
    """B2 Part 3.5 — a WARNING string if resuming more than `max_days`
    after the LAST segment's `ended_at` (the actual pause moment, not
    campaign start), else None. Never refuses (unlike config_drift_diff):
    real sites / live ground truths may have moved, worth recording, not
    worth blocking a resume over."""
    segments = state.get("segments") or []
    if not segments or not segments[-1].get("ended_at"):
        return None
    paused_at = datetime.fromisoformat(segments[-1]["ended_at"])
    elapsed_days = (now() - paused_at).total_seconds() / 86400
    if elapsed_days <= max_days:
        return None
    return (
        f"reprise {elapsed_days:.1f} jours après la pause (seuil {max_days}) — "
        "les cibles réelles (sites vivants, sonde de péremption D2) ont pu changer"
    )


def normalize_duration_estimate(value) -> dict:
    """Pre-B2 DURATION_ESTIMATE_CACHE.json entries are a bare float (single
    median, no range) — read as a degenerate {median,min,max,n=1} rather
    than forcing an upfront migration of the tracked JSON file. Shared by
    test_web_tasks.py (writing the cache) and compute_remaining_eta()
    below/the dashboard (reading it)."""
    if isinstance(value, dict):
        return value
    return {"median": value, "min": value, "max": value, "n": 1}


def compute_remaining_eta(state: dict, estimates: dict) -> dict:
    """B2 Part 1.4: sum, over each REMAINING run, of ITS task's expected
    duration — never a global median applied to all remaining runs, which
    drifts with execution order across v2's deliberately heterogeneous
    task lengths (family A runs minutes, family F is short).

    `estimates` is DURATION_ESTIMATE_CACHE.json's "estimates" dict. A task
    with no entry (cold start, never measured) is counted in
    `unreliable_task_count` and excluded from the totals — the caller
    (dashboard) must render "estimate unreliable" rather than a confident
    number when this is nonzero (Part 1.4's cold-start requirement), not
    silently substitute a default duration into an ETA."""
    planned = state.get("planned", [])
    remaining = planned[len(state.get("completed", [])):]

    median_total = min_total = max_total = 0.0
    unreliable_tasks = set()
    for entry in remaining:
        task_id = entry["task_id"]
        raw = estimates.get(task_id)
        if raw is None:
            unreliable_tasks.add(task_id)
            continue
        entry = normalize_duration_estimate(raw)
        median_total += entry["median"]
        min_total += entry["min"]
        max_total += entry["max"]

    return {
        "remaining_runs": len(remaining),
        "median_seconds": round(median_total, 1),
        "min_seconds": round(min_total, 1),
        "max_seconds": round(max_total, 1),
        "unreliable_task_count": len(unreliable_tasks),
        "reliable": not unreliable_tasks,
    }


def write_progress_json(path: Path, state: dict) -> None:
    """Rewritten atomically (temp + os.replace) at every run boundary — B2
    Part 1.1: a campaign killed mid-flight keeps everything up to the last
    completed run, unlike write_campaign_json() above which is a single
    end-of-campaign artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
