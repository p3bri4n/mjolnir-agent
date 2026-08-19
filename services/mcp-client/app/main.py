"""
MCP Client: LangGraph Agent's single entry point to the MCP servers.

The official mcp/filesystem image communicates over STDIO: this service
spawns it on demand, via `docker run -i --rm ...` on the Docker socket
mounted from the host, rather than treating it as a persistent network
server.

⚠️ Mounting /var/run/docker.sock into a container is equivalent to giving
it root access on the host (the container can launch any other
container, including privileged ones). In production, prefer a Docker
socket proxy alternative (e.g. tecnativa/docker-socket-proxy) that
restricts allowed operations (only `create`/`start`/`attach` on
whitelisted images), rather than exposing the raw socket.

"browser" (Playwright) is different: it's a persistent HTTP server (see
docker-compose.yml's `playwright-mcp` service), and mcp-client connects
to it over Streamable HTTP instead of spawning a container.

git/terminal/desktop(GhostDesk)/ocr were removed from this registry
(docs/briefs/update-plan.md effort 1.2, docs/history.md): schema-weight
audit found desktop+git+ocr+terminal cost 44.9% of the tool schema for
1.6% of real usage across 67 v2 campaign threads. GhostDesk itself was
later removed entirely (effort 3, docs/history.md). ocr-service is not
a tool server and never will be reachable through this registry — it's
a graph-internal capability, called directly by langgraph-agent over
plain HTTP (see docs/architecture/autonomy.md).
"""

import asyncio
import base64
import io
import json
import os
import re
from collections import defaultdict
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from PIL import Image
from pydantic import BaseModel
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

WORKSPACE_HOST_PATH = os.environ.get("WORKSPACE_HOST_PATH", "./workspace")

# Visual feedback (docs/briefs/campaign-visual-feedback.md, minimal subset,
# see that file's "Status" section for the full design and the two
# deviations from its literal text — thread_id keying, workspace/ path).
# Off by default: overhead not yet measured (point 6 of the implementation
# instruction) — flip only after a with/without smoke names a real number.
CAMPAIGN_VISUAL_CAPTURE = os.environ.get("CAMPAIGN_VISUAL_CAPTURE", "false").lower() == "true"
VISUAL_CAPTURE_DIR = Path(os.environ.get("VISUAL_CAPTURE_DIR", "/visual-capture"))
VISUAL_CAPTURE_JPEG_QUALITY = 60

SERVERS = {
    "filesystem": {
        "transport": "stdio",
        "params": StdioServerParameters(
            command="docker",
            args=[
                "run", "-i", "--rm",
                "-v", f"{WORKSPACE_HOST_PATH}:/projects",
                # Volume SHARED READ-ONLY with playwright-mcp (see
                # docker-compose.yml, --output-dir): gives the agent a
                # DOCUMENTED read path for a file downloaded by the
                # browser, rather than guessing a path internal to the
                # playwright-mcp container (see docs/history.md "revised
                # Phase 1d", T5). ":ro" because this server must never be
                # able to write into the web agent's downloads.
                "-v", "agent-downloads:/downloads:ro",
                os.environ.get("MCP_FILESYSTEM_IMAGE", "mcp/filesystem:latest"),
                "/projects",
                "/downloads",
            ],
        ),
    },
    "browser": {
        # Unlike "filesystem" above, "browser" is a persistent HTTP
        # server: an ephemeral spawn (`docker run --rm` per call) would
        # restart a brand-new browser
        # on EVERY tool call, with no state continuity between
        # `browser_navigate` and the next call — see docs/resolved-bugs.md.
        # The official mcp/playwright image supports a native HTTP server
        # mode (`--port`, Streamable HTTP `/mcp` endpoint), used here via
        # the dedicated `playwright-mcp` docker-compose service.
        "transport": "http",
        "url": os.environ.get("MCP_PLAYWRIGHT_URL", "http://playwright-mcp:8931/mcp"),
        "token": "",
        # Playwright MCP scopes its browser context (page, cookies,
        # history) to the MCP SESSION, not the server process: going
        # through an ephemeral session per call (like the other http
        # servers) recreates an `about:blank` every time even once the
        # server is made persistent (verified empirically). Hence the
        # need to keep a session open across calls, see
        # `_get_persistent_session` below.
        "persistent_session": True,
    },
}

# TARGETED LOCATION/EXTRACTION tool (revised Phase 1d, see
# docs/history.md "extraction fix"): the official Playwright MCP exposes
# no "search this text and give its context" tool (verified:
# browser_click/hover/select_option all require an already-located
# target; only browser_evaluate/browser_run_code_unsafe allow searching,
# at the cost of arbitrary JS code, ENGAGEMENT tier). Observed under real
# conditions (T1/T10, post-1d campaign) that making these two tools never
# session-grantable (see approval_policy.NEVER_GRANTABLE_TOOLS on the
# langgraph-agent side) made their usage disappear — replaced by manual
# exploration (ctrl+f, page-by-page browsing) noticeably less reliable.
# The CLEAN PATH gets the crutch's capability here: a FIXED JS TEMPLATE
# (never model-supplied code, only a text to search, interpolated via
# json.dumps — hence escaped as a valid JS string, no code injection
# possible) that walks the page's text nodes and returns the occurrences
# with their nearby context (parent text, enclosing link if any). The
# model never sees this template, only the "query" parameter.
#
# adjacent_value (docs/briefs/update-plan.md 2.3, A1 trajectory diagnostic,
# docs/history.md): the walker above matches a LABEL text node
# ("Référence", "Prix") but, before this field existed, never returned the
# structured VALUE next to it — confirmed verbatim in the model's own
# reasoning ("le mot 'Prix' est trouvé mais pas la valeur"), which forced a
# `browser_run_code_unsafe` (NEVER_GRANTABLE) workaround on A2 and an
# 8-turn per-page re-navigation fallback on A1. Fixtures checked before
# writing this (not guessed): `dt`/`dd` (catalog product pages) and `td`
# siblings within the same `tr` (docs parameter table, hr-app listings)
# are the two patterns actually used; `label`/`input` was considered and
# dropped — every fixture `<input>` is an unfilled form field, never a
# value to read.
_BROWSER_EXTRACT_JS_TEMPLATE = """() => {{
  const query = {query_json};
  const q = query.toLowerCase();
  const results = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {{
    const text = (node.textContent || '').trim();
    if (!text || !text.toLowerCase().includes(q)) continue;
    const parent = node.parentElement;
    const link = parent ? parent.closest('a') : null;
    let adjacent_value = null;
    if (parent) {{
      const tag = parent.tagName.toLowerCase();
      if (tag === 'dt' && parent.nextElementSibling && parent.nextElementSibling.tagName.toLowerCase() === 'dd') {{
        adjacent_value = parent.nextElementSibling.textContent.trim();
      }} else if (tag === 'td' || tag === 'th') {{
        const row = parent.closest('tr');
        if (row) {{
          const cells = Array.from(row.children).filter((c) => c !== parent);
          adjacent_value = cells.map((c) => c.textContent.trim()).join(' | ');
        }}
      }}
    }}
    results.push({{
      text: text.slice(0, 300),
      parent_tag: parent ? parent.tagName.toLowerCase() : null,
      parent_text: parent ? parent.textContent.trim().slice(0, 300) : null,
      link_href: link ? link.getAttribute('href') : null,
      adjacent_value: adjacent_value,
    }});
    if (results.length >= 20) break;
  }}
  return JSON.stringify(results);
}}"""


# Bulk mode (found while investigating T1, see docs/history.md
# "BULK_CHECK_DIRECTIVE"): when the sought information only appears on
# DETAIL pages (never the listing) and several need checking, a
# page-by-page navigation exhausts the iteration budget before everything
# is even checked. The prompt instruction used to push the model into
# writing its own fetch() loop via browser_evaluate (TIER_SENSITIVE,
# NEVER_GRANTABLE, arbitrary JS code) — it worked, but it's fragile (the
# model must write correct JS every time) for a need that never actually
# required arbitrary code, only a request across SEVERAL pages instead of
# one. `urls` (optional) gives this capability at TIER_READ, the same
# FIXED template as the single-page search: fetch() + DOMParser + the
# same text-node walk, per URL. Failure on an individual URL (network,
# cross-origin CORS) captured per page, never propagated — one
# unreachable page must not invalidate the whole batch.
_BROWSER_EXTRACT_BULK_JS_TEMPLATE = """async () => {{
  const query = {query_json};
  const urls = {urls_json};
  const q = query.toLowerCase();
  const MAX_PER_PAGE = 20;
  const MAX_URLS = 50;
  function extractFrom(doc) {{
    const results = [];
    const walker = doc.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {{
      const text = (node.textContent || '').trim();
      if (!text || !text.toLowerCase().includes(q)) continue;
      const parent = node.parentElement;
      const link = parent ? parent.closest('a') : null;
      let adjacent_value = null;
      if (parent) {{
        const tag = parent.tagName.toLowerCase();
        if (tag === 'dt' && parent.nextElementSibling && parent.nextElementSibling.tagName.toLowerCase() === 'dd') {{
          adjacent_value = parent.nextElementSibling.textContent.trim();
        }} else if (tag === 'td' || tag === 'th') {{
          const row = parent.closest('tr');
          if (row) {{
            const cells = Array.from(row.children).filter((c) => c !== parent);
            adjacent_value = cells.map((c) => c.textContent.trim()).join(' | ');
          }}
        }}
      }}
      results.push({{
        text: text.slice(0, 300),
        parent_tag: parent ? parent.tagName.toLowerCase() : null,
        parent_text: parent ? parent.textContent.trim().slice(0, 300) : null,
        link_href: link ? link.getAttribute('href') : null,
        adjacent_value: adjacent_value,
      }});
      if (results.length >= MAX_PER_PAGE) break;
    }}
    return results;
  }}
  const targets = urls.slice(0, MAX_URLS);
  const matches = {{}};
  const errors = {{}};
  for (const url of targets) {{
    try {{
      const resp = await fetch(url);
      const html = await resp.text();
      const doc = new DOMParser().parseFromString(html, 'text/html');
      const found = extractFrom(doc);
      if (found.length) matches[url] = found;
    }} catch (e) {{
      errors[url] = String(e);
    }}
  }}
  return JSON.stringify({{ checked: targets.length, matches, errors }});
}}"""


def _build_extract_function(query: str, urls: list = None) -> str:
    """Pure function (testable with no real MCP server): builds the fixed
    JS (single-page by default, bulk if `urls` is non-empty) with `query`/
    `urls` interpolated via `json.dumps` — JSON string/array syntax is
    valid JS syntax, so this escaping is enough to prevent any literal
    escape (quotes, backslashes, newlines in the query or a URL)."""
    if urls:
        return _BROWSER_EXTRACT_BULK_JS_TEMPLATE.format(query_json=json.dumps(query), urls_json=json.dumps(urls))
    return _BROWSER_EXTRACT_JS_TEMPLATE.format(query_json=json.dumps(query))


_BROWSER_EXTRACT_TOOL = {
    "server": "browser",  # dispatched internally to browser_evaluate, see call_tool()
    "description": (
        "Cherche un TEXTE (pas du code) dans la page actuelle — référence "
        "produit, prix, nom, mot-clé — et renvoie les occurrences avec leur "
        "contexte proche (texte du parent, lien englobant si présent, "
        "valeur adjacente si le texte trouvé est un label structuré : "
        "value du <dd> pour une paire <dt>/<dd>, autres cellules de la "
        "ligne pour un <td>/<th> de tableau — ne re-navigue jamais pour "
        "lire cette valeur, elle est déjà dans le résultat). "
        "Pour trouver une valeur précise dans une page, utilise CET outil : "
        "pas de parcours manuel page par page, pas de raccourci "
        "clavier de recherche (ctrl+f). Si l'information n'apparaît que sur "
        "des pages de DÉTAIL et qu'il faut en vérifier PLUSIEURS (ex. 30 "
        "fiches produit) pour la trouver, passe leurs URL dans `urls` en UN "
        "seul appel plutôt que de naviguer puis appeler cet outil page par "
        "page — bien plus économe en itérations."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Texte exact ou partiel à chercher (ex: une référence produit, un nom).",
            },
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optionnel : URL de PLUSIEURS pages à vérifier en un seul appel "
                    "(mode bulk) plutôt que la seule page actuelle. Chaque URL est "
                    "récupérée indépendamment (fetch) ; une URL injoignable n'invalide "
                    "pas les autres. Plafonné à 50 URL par appel."
                ),
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

# ref= NORMALIZATION (2026-07-31, see docs/resolved-bugs.md "défaut ref=
# browser_fill_form", live since 2026-07-22 across every fixture): the
# official Playwright MCP's targetLocator treats a `target` string as an
# aria-ref ONLY if it matches ^(f\d+)?e\d+$ (bare "e7", optionally
# "f2e7") — anything else is parsed as a CSS/engine selector. The model
# routinely copies the annotation verbatim from browser_snapshot's own
# output ("[ref=e7]"), producing "ref=e7", which Playwright's selector
# parser reads as an unknown engine named "ref" and rejects outright
# (100% failure rate measured across every historical browser_fill_form
# call using this form). Normalizing here — before dispatch — fixes the
# defect at the mechanism, for every current and future tool that carries
# a target/startTarget/endTarget (click, hover, drag, fill_form's nested
# fields, select_option, evaluate's element-scoped form...), without a
# per-tool list to keep in sync.
_REF_TARGET_KEYS = {"target", "startTarget", "endTarget"}
_REF_PREFIX_RE = re.compile(r"^ref=((?:f\d+)?e\d+)$")


def _normalize_ref_value(value):
    if isinstance(value, str):
        match = _REF_PREFIX_RE.match(value)
        if match:
            return match.group(1)
    return value


def _normalize_ref_targets(obj):
    """Recursively rewrites "ref=eN"-style values found under
    target/startTarget/endTarget keys to the bare "eN" token Playwright
    expects. Walks nested dicts/lists so browser_fill_form's `fields`
    array is covered by the same pass, no special-casing needed."""
    if isinstance(obj, dict):
        return {
            key: (_normalize_ref_value(value) if key in _REF_TARGET_KEYS else _normalize_ref_targets(value))
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [_normalize_ref_targets(item) for item in obj]
    return obj


# Filet pour toute forme non reconnue par la normalisation ci-dessus
# (ex. casse différente, préfixe inattendu) : rediriger vers la bonne
# syntaxe plutôt que renvoyer le message d'erreur brut de Playwright, qui
# ne dit pas au modèle comment se corriger.
_UNKNOWN_REF_ENGINE_RE = re.compile(r'Unknown engine "\w+" while parsing selector')

_FRIENDLY_REF_ERROR = (
    "Erreur : référence invalide. Utilise le jeton nu du dernier "
    "browser_snapshot (ex. \"e7\", sans le préfixe \"ref=\") ou un "
    "sélecteur CSS (ex. \"input[name='...']\")."
)


def _rewrite_ref_error(content_blocks):
    for block in content_blocks:
        text = getattr(block, "text", None)
        if text and _UNKNOWN_REF_ENGINE_RE.search(text):
            block.text = _FRIENDLY_REF_ERROR
    return content_blocks


# EMPTY-SNAPSHOT REDIRECT (docs/history.md, "PROBE VISUEL — SIGNAL
# BROWSER_SNAPSHOT"): of the 4 visual-only patterns probed against
# fixture-visual-probe, only ONE (a page navigated to directly as a
# native PDF) produces a distinctive signal — the response's own
# "```yaml ... ```" block comes back entirely empty (no node at all, not
# even a page title line above it), unlike canvas/WebGL/alt-less-img
# which sit on an otherwise normal page and leave no trace to grep for.
# This is a genuine, structural signal (verified against the real
# Playwright MCP server's own response format, CLAUDE.md #8), not a
# guess — a redirect hint, not a block: the tool call still succeeds and
# returns its (empty) result, this only appends guidance.
_SNAPSHOT_YAML_RE = re.compile(r"```yaml\n(.*?)```", re.DOTALL)

_EMPTY_SNAPSHOT_HINT = (
    "\n\n(Snapshot vide — aucun contenu accessible sur cette page. Cas "
    "vérifié : PDF affiché nativement dans le navigateur, ou tout autre "
    "contenu hors de l'arbre d'accessibilité. browser_take_screenshot "
    "peut être nécessaire.)"
)


def _is_empty_snapshot_text(text: str) -> bool:
    match = _SNAPSHOT_YAML_RE.search(text)
    return bool(match) and not match.group(1).strip()


def _flag_empty_snapshot(content_blocks):
    for block in content_blocks:
        text = getattr(block, "text", None)
        if text and _is_empty_snapshot_text(text):
            block.text = text + _EMPTY_SNAPSHOT_HINT
    return content_blocks


# CAPACITÉ D'INTROSPECTION MANQUANTE (même diagnostic que ci-dessus,
# 2026-07-31) : une fois le défaut ref= corrigé, le repli légitime du
# modèle face à un sélecteur qu'il ignore encore reste l'introspection du
# DOM — jusqu'ici seulement possible via browser_evaluate (TIER_SENSITIVE,
# NEVER_GRANTABLE, code arbitraire), cause directe de la brèche B-β hard
# (docs/campaigns/2026-07-30_campaign-v2_b2-mesure-medium-hard.md). Même
# mouvement que browser_extract : un template JS FIXE (jamais de code
# fourni par le modèle), tier LECTURE. Cible un élément précis (target,
# résolu par browser_evaluate lui-même comme locator) ou, à défaut,
# recense tous les champs de formulaire de la page.
_BROWSER_INSPECT_JS_SINGLE = """(el) => {
  const label = el.labels && el.labels[0] ? el.labels[0].textContent.trim() : null;
  return JSON.stringify({
    tag: el.tagName.toLowerCase(),
    type: el.type || null,
    name: el.name || null,
    id: el.id || null,
    placeholder: el.placeholder || null,
    label: label,
  });
}"""

_BROWSER_INSPECT_JS_ALL = """() => {
  const els = document.querySelectorAll('input, select, textarea, button');
  const attrs = Array.from(els).map((el) => {
    const label = el.labels && el.labels[0] ? el.labels[0].textContent.trim() : null;
    return {
      tag: el.tagName.toLowerCase(),
      type: el.type || null,
      name: el.name || null,
      id: el.id || null,
      placeholder: el.placeholder || null,
      label: label,
    };
  });
  return JSON.stringify(attrs);
}"""


def _build_inspect_call(target: str = None, element: str = None) -> tuple[str, dict]:
    """Pure function (testable with no real MCP server): returns the
    (tool, arguments) pair to dispatch to browser_evaluate — element-scoped
    template if `target` is given (normalized the same way as every other
    ref-bearing argument), page-wide form scan otherwise."""
    if target:
        normalized = _normalize_ref_value(target)
        return "browser_evaluate", {
            "function": _BROWSER_INSPECT_JS_SINGLE,
            "target": normalized,
            "element": element or "élément à inspecter",
        }
    return "browser_evaluate", {"function": _BROWSER_INSPECT_JS_ALL}


_BROWSER_INSPECT_TOOL = {
    "server": "browser",  # dispatched internally to browser_evaluate, see call_tool()
    "description": (
        "Renvoie les attributs RÉELS des champs d'un formulaire (name, id, "
        "type, placeholder, label associé) — utilise cet outil quand un "
        "sélecteur (ref ou description) ne fonctionne pas sur browser_click/"
        "browser_fill_form/browser_type, avant d'en essayer un autre. Sans "
        "`target`, recense tous les champs de la page ; avec `target` (une "
        "ref de browser_snapshot ou un sélecteur CSS), détaille cet élément "
        "précis. Ne prend jamais de code."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": (
                    "Optionnel : ref du dernier browser_snapshot (ex. \"e7\") ou "
                    "sélecteur CSS de l'élément à inspecter. Omis : recense tous "
                    "les champs de formulaire de la page."
                ),
            },
            "element": {
                "type": "string",
                "description": "Optionnel : description humaine de l'élément ciblé par `target`.",
            },
        },
        "additionalProperties": False,
    },
}

app = FastAPI(title="MCP Client")

# registry {tool_name: {"server", "description", "inputSchema"}}, lazily
# built on the 1st call (description/inputSchema needed so langgraph-agent
# can bind these tools to the LLM via bind_tools — without which the
# model simply has no idea these tools exist).
_tool_registry: dict[str, dict] = {}

# MCP sessions kept open across HTTP calls, for servers where state
# (browser, page) lives in the session rather than the server process —
# see "persistent_session" on the "browser" entry above.
#
# Keyed by (server_name, worker_id) — effort 1.3 (docs/briefs/
# effort-1.3-parallel-campaigns.md): Playwright MCP scopes its browser
# context (page, cookies, history) to the MCP SESSION, not the process
# (docs/resolved-bugs.md, session-continuity entry, verified against the
# installed image) — a distinct worker_id therefore gets a genuinely
# isolated browser context for free, confirmed live
# (scripts/probe-parallel-phase0.sh, Phase 0). `worker_id` is always
# normalized through `_worker_key` before touching either dict below: a
# missing/empty worker_id maps to the SAME `"default"` bucket every
# existing caller (interactive Open WebUI, a non-parallel campaign) has
# always used — zero behavior change for anyone who doesn't opt in.
_persistent_sessions: dict[tuple[str, str], tuple[AsyncExitStack, ClientSession]] = {}
# Lazily created per (server_name, worker_id): worker_id is arbitrary
# caller-supplied text, not enumerable up front like SERVERS is. Safe
# without an extra guard lock — defaultdict's __missing__ runs
# synchronously (no `await` inside it), so two concurrent coroutines
# requesting the same NEW key on this single-process/single-event-loop
# service (see Dockerfile: uvicorn, no --workers) can never race each
# other into creating two different Lock objects for it.
_persistent_locks: dict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)


def _worker_key(worker_id: Optional[str]) -> str:
    return worker_id or "default"


def _http_headers(server: dict) -> dict:
    headers = {}
    if server.get("token"):
        headers["Authorization"] = f"Bearer {server['token']}"
    return headers


async def _open_session(server_name: str, stack: AsyncExitStack) -> ClientSession:
    server = SERVERS[server_name]
    if server["transport"] == "stdio":
        read, write = await stack.enter_async_context(stdio_client(server["params"]))
    else:
        read, write, _ = await stack.enter_async_context(
            streamablehttp_client(server["url"], headers=_http_headers(server))
        )
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return session


async def _get_persistent_session(server_name: str, worker_id: Optional[str] = None) -> ClientSession:
    """Reuses this worker's existing session if alive, opens a new one otherwise."""
    key = (server_name, _worker_key(worker_id))
    async with _persistent_locks[key]:
        cached = _persistent_sessions.get(key)
        if cached is not None:
            return cached[1]
        stack = AsyncExitStack()
        try:
            session = await _open_session(server_name, stack)
        except Exception:
            await stack.aclose()
            raise
        _persistent_sessions[key] = (stack, session)
        return session


async def _drop_persistent_session(server_name: str, worker_id: Optional[str] = None) -> None:
    cached = _persistent_sessions.pop((server_name, _worker_key(worker_id)), None)
    if cached is not None:
        await cached[0].aclose()


async def _run_on_server(server_name: str, action, worker_id: Optional[str] = None):
    """Runs `action` on the server: persistent session if configured, ephemeral otherwise."""
    server = SERVERS[server_name]
    if server.get("persistent_session"):
        session = await _get_persistent_session(server_name, worker_id)
        try:
            return await action(session)
        except Exception:
            # connection probably dead (server restarted...): drop it, the
            # next call will reopen a fresh one rather than staying stuck
            await _drop_persistent_session(server_name, worker_id)
            raise
    async with AsyncExitStack() as stack:
        session = await _open_session(server_name, stack)
        return await action(session)


@app.on_event("shutdown")
async def _close_persistent_sessions():
    for server_name, worker_id in list(_persistent_sessions):
        await _drop_persistent_session(server_name, worker_id)


def _write_visual_capture(data_b64: str, thread_id: str) -> None:
    """
    Re-encodes to JPEG q60 regardless of the source format (Playwright's
    own default is WebP, see app/graph.py's IMAGE_FORMAT_PASSTHROUGH
    comment) — guarantees the target format without depending on
    browser_take_screenshot's exact parameter support, which isn't
    reliably introspectable ahead of the live image (verify against the
    running playwright-mcp image if that ever needs tightening).
    Atomic write (temp + os.replace, same directory/filesystem) so the
    dashboard never serves a half-written file.
    """
    raw = base64.b64decode(data_b64)
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    out_dir = VISUAL_CAPTURE_DIR / thread_id
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = out_dir / "latest.jpg.tmp"
    final_path = out_dir / "latest.jpg"
    img.save(tmp_path, format="JPEG", quality=VISUAL_CAPTURE_JPEG_QUALITY)
    os.replace(tmp_path, final_path)


async def _maybe_capture_visual(thread_id: Optional[str], worker_id: Optional[str] = None) -> None:
    """
    Best-effort observability side channel (docs/briefs/
    campaign-visual-feedback.md): fires an INTERNAL follow-up
    browser_take_screenshot call on the same persistent "browser" session
    (same precedent as the existing stabilization browser_wait_for call
    below) and writes it straight to disk — never appended to any /call
    response, never seen by langgraph-agent, structurally unable to reach
    the model's context (see tests/test_main.py's non-negotiable test).
    No-op when the flag is off or no thread_id was supplied (e.g. the
    verification-snapshot helper in app/graph.py, which has no thread_id
    in scope) — never allowed to fail the real tool call it rides along.
    `worker_id` (effort 1.3) must match the request it rides along: an
    unscoped capture would silently screenshot a DIFFERENT worker's page.
    """
    if not CAMPAIGN_VISUAL_CAPTURE or not thread_id:
        return
    try:
        result = await _run_on_server(
            "browser", lambda s: s.call_tool("browser_take_screenshot", {}), worker_id
        )
        for block in result.content:
            if getattr(block, "type", None) == "image":
                _write_visual_capture(block.data, thread_id)
                break
    except Exception:
        pass


# Routing hint for browser_take_screenshot (docs/history.md, "PROBE
# VISUEL — SIGNAL BROWSER_SNAPSHOT"): a direct empirical probe against
# fixture-visual-probe (canvas 2D, WebGL, alt-less img, native PDF)
# showed these elements leave ZERO trace in browser_snapshot's
# accessibility-tree text — a page with a canvas is text-identical to one
# without. No after-the-fact heuristic can catch this (a role:"img" match
# was tried against a control case and produced a proven false positive
# on inline SVG text, which needs no OCR at all) — the routing decision
# has to happen BEFORE the fact, in the tool's own description, so the
# model knows to reach for this tool when browser_snapshot comes up
# short. Appended to whatever the real Playwright server declares, never
# replacing it (upstream wording may change independently).
_TOOL_DESCRIPTION_APPENDS = {
    "browser_take_screenshot": (
        " Utilise cet outil si browser_snapshot ne contient pas "
        "l'information attendue : contenu affiché via <canvas> ou WebGL, "
        "image sans texte alternatif, ou PDF affiché directement dans le "
        "navigateur — ces cas n'apparaissent dans AUCUN arbre "
        "d'accessibilité, browser_snapshot ne le signale pas lui-même."
    ),
}


def _tool_description_with_appends(name: str, description: str) -> str:
    """Pure function (testable with no real MCP server): appends this
    project's own routing hint, if any, to whatever the upstream server
    declared — never replaces it."""
    return description + _TOOL_DESCRIPTION_APPENDS.get(name, "")


async def _refresh_registry():
    for server_name in SERVERS:
        try:
            tools = await _run_on_server(server_name, lambda s: s.list_tools())
            for tool in tools.tools:
                _tool_registry[tool.name] = {
                    "server": server_name,
                    "description": _tool_description_with_appends(tool.name, tool.description or ""),
                    "inputSchema": tool.inputSchema or {"type": "object", "properties": {}},
                }
        except Exception:
            # an unavailable server must not block the others from starting
            continue
    # Synthetic tool (see _BROWSER_EXTRACT_TOOL above): doesn't exist on
    # any real MCP server, added afterward so it's never overwritten by a
    # refresh, which only ever sees the real servers.
    if "browser" in SERVERS:
        _tool_registry["browser_extract"] = _BROWSER_EXTRACT_TOOL
        _tool_registry["browser_inspect"] = _BROWSER_INSPECT_TOOL


class CallRequest(BaseModel):
    tool: str
    arguments: dict = {}
    # Visual feedback (docs/briefs/campaign-visual-feedback.md): optional,
    # only used to key _maybe_capture_visual's output directory — absent
    # for callers that don't have one (e.g. app/graph.py's
    # _fetch_verification_snapshot), which simply skips the capture.
    thread_id: Optional[str] = None
    # Persistent-session isolation (effort 1.3, docs/briefs/
    # effort-1.3-parallel-campaigns.md): absent for every existing caller
    # (interactive Open WebUI, a non-parallel campaign) — same shared
    # "default" session as before _worker_key normalizes it. Only a
    # parallel campaign runner sets this, one distinct value per worker.
    worker_id: Optional[str] = None


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/reset-session/{server_name}")
async def reset_session(server_name: str, worker_id: Optional[str] = None):
    """
    Explicit reset of a PERSISTENT session (revised Phase 1d, see
    docs/history.md "cross-task isolation"): drops the cached session
    (`_drop_persistent_session`), the next call will reopen a fresh one.
    Without this entry point, only a full service restart (or a
    fortuitous exception during a call) would purge a persistent
    session's state — for "browser" (playwright-mcp), this meant tabs/
    URLs left open from one task to the next, silently visible in the
    NEXT task's snapshot (observed under real conditions: a "Science |
    Books to Scrape" tab left open after T10 polluted T7's snapshot in a
    later repetition, several campaigns/hours later).
    404 if `server_name` isn't configured for a persistent session
    (nothing to reset) rather than a silent no-op — prevents a misspelled
    server name from going unnoticed on the caller side (the web-task
    harness, see tests_integration/test_web_tasks.py).

    `worker_id` (effort 1.3, optional query param): resets only that
    worker's session. Omitted, resets the shared "default" session — the
    exact pre-effort-1.3 behavior, unchanged for every caller that
    doesn't pass it. Without this parameter, one worker's cross-task
    reset would blow away every other worker's live browser session
    mid-campaign.
    """
    if not SERVERS.get(server_name, {}).get("persistent_session"):
        raise HTTPException(
            status_code=404,
            detail=f"'{server_name}' n'est pas configuré en session persistante.",
        )
    await _drop_persistent_session(server_name, worker_id)
    return {"status": "reset"}


@app.get("/tools")
async def list_all_tools():
    """{tool_name: server_name} — simple view used for inspection/debugging."""
    await _refresh_registry()
    return {"tools": {name: info["server"] for name, info in _tool_registry.items()}}


@app.get("/tools/schema")
async def list_tools_schema():
    """
    Schema in OpenAI function-calling format (used by langgraph-agent to
    bind tools to the LLM via bind_tools — see app/graph.py).
    """
    await _refresh_registry()
    return {
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": info["description"],
                    "parameters": info["inputSchema"],
                },
            }
            for name, info in _tool_registry.items()
        ]
    }


# Post-navigation stabilization (found while investigating T10, see
# docs/history.md "snapshot/URL desync"): on a client-rendered page (e.g.
# books.toscrape.com, category loaded after the click), `browser_snapshot`
# can return the OLD page's content while the URL/screenshot already
# confirm the change — the agent then loses several turns convincing
# itself of where it is, sometimes exhausting its iteration budget before
# even seeing the useful content. `browser_wait_for` (a real mcp/playwright
# tool, confirmed via GET /tools/schema: time/text/textGone) is called
# automatically after EVERY successful browser_navigate/browser_click,
# transparent to the agent — no text expected in advance, a short fixed
# delay is enough to let client-side rendering settle. A server-side fix
# rather than a prompt instruction: a fixed delay doesn't depend on any
# model behavior to be applied.
_STABILIZE_AFTER_TOOLS = {"browser_navigate", "browser_click"}
BROWSER_STABILIZE_WAIT_SECONDS = float(os.environ.get("BROWSER_STABILIZE_WAIT_SECONDS", "0.5"))


@app.post("/call")
async def call_tool(request: CallRequest):
    if request.tool not in _tool_registry:
        await _refresh_registry()
    tool_info = _tool_registry.get(request.tool)
    if not tool_info:
        raise HTTPException(status_code=404, detail=f"Outil inconnu : {request.tool}")

    arguments = _normalize_ref_targets(request.arguments)

    if request.tool == "browser_extract":
        # Dispatched internally to browser_evaluate with a FIXED JS
        # template (see _build_extract_function): the model never
        # supplies code, only the text to search for (and, in bulk mode,
        # the list of URLs to check).
        js_function = _build_extract_function(arguments.get("query", ""), arguments.get("urls"))
        result = await _run_on_server(
            "browser", lambda s: s.call_tool("browser_evaluate", {"function": js_function}), request.worker_id
        )
        await _maybe_capture_visual(request.thread_id, request.worker_id)
        return {"content": [block.model_dump() for block in result.content]}

    if request.tool == "browser_inspect":
        eval_tool, eval_args = _build_inspect_call(arguments.get("target"), arguments.get("element"))
        result = await _run_on_server("browser", lambda s: s.call_tool(eval_tool, eval_args), request.worker_id)
        await _maybe_capture_visual(request.thread_id, request.worker_id)
        return {"content": [block.model_dump() for block in _rewrite_ref_error(result.content)]}

    result = await _run_on_server(
        tool_info["server"], lambda s: s.call_tool(request.tool, arguments), request.worker_id
    )
    extra_content = []
    if request.tool in _STABILIZE_AFTER_TOOLS and BROWSER_STABILIZE_WAIT_SECONDS > 0:
        await _run_on_server(
            "browser",
            lambda s: s.call_tool("browser_wait_for", {"time": BROWSER_STABILIZE_WAIT_SECONDS}),
            request.worker_id,
        )
        # Tool design contract (CLAUDE.md): browser_navigate/browser_click's
        # own response never contains the resulting page — only a
        # "### Snapshot\n- [Snapshot](../../downloads/....yml)" reference to
        # a file the agent has no tool to read (verified against the real
        # audit log). A real browser_snapshot call, now that the page has
        # stabilized, gives the actual resulting state instead of a dead
        # reference — same _run_on_server dispatch already used for
        # browser_extract/browser_inspect above.
        snapshot_result = await _run_on_server(
            "browser", lambda s: s.call_tool("browser_snapshot", {}), request.worker_id
        )
        extra_content = list(snapshot_result.content)
    # Captured for every "browser" tool (not just navigate/click): a
    # snapshot/extract/fill_form call is still "what the agent is looking
    # at" for the dashboard's purposes (docs/briefs/
    # campaign-visual-feedback.md, §1's DOM-first framing).
    if tool_info["server"] == "browser":
        await _maybe_capture_visual(request.thread_id, request.worker_id)
    content_blocks = _rewrite_ref_error(list(result.content) + extra_content)
    if request.tool == "browser_snapshot" or extra_content:
        content_blocks = _flag_empty_snapshot(content_blocks)
    return {"content": [block.model_dump() for block in content_blocks]}
