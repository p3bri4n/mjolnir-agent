"""
OCR Service: an HTTP MCP server (Streamable HTTP, like GhostDesk) that
gives the agent EXACT text coordinates on the GhostDesk desktop,
complementing the VLM (Qwen3.6 MoE), which reasons well but localizes
poorly (imprecise grounding from a general-purpose vision model with no
dedicated UI-element detection — see README, Accepted known limitations
section).

Capture: this service connects itself over Streamable HTTP to the
GhostDesk MCP server (internal agent-net network, bearer
GHOSTDESK_AUTH_TOKEN), exactly as mcp-client does for the "desktop"
server — NO image passes through mcp-client or the LLM for this flow,
entirely internal to ocr-service. Explicit format="png": ocr-service
never depends on the llama-server fork's native WebP decoding, not
relevant here (no LLM in this loop).

Two tools exposed to langgraph-agent (via mcp-client, see Phase 2):
  - find_text(query, fuzzy=True): coordinates of matches, sorted by
    decreasing confidence, empty list if none (never an error).
  - read_screen(): all detected text, capped at ~80 elements.

Both are TIER_READ on the langgraph-agent side (approval_policy.py): pure
read, no side effect, nothing to exfiltrate.
"""

import base64
import io
import os

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.fastmcp import FastMCP
from PIL import Image
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.coords import convert_detection
from app.matching import matches
from app.ocr_engine import get_engine

GHOSTDESK_URL = os.environ.get("MCP_GHOSTDESK_URL", "http://ghostdesk:3000/mcp")
GHOSTDESK_AUTH_TOKEN = os.environ.get("GHOSTDESK_AUTH_TOKEN", "")
OCR_AUTH_TOKEN = os.environ.get("OCR_AUTH_TOKEN", "")

# See app/coords.py: "1000" (default) converts to the GhostDesk/
# mouse_click normalized coordinate space, "pixels" disables conversion.
OCR_COORD_SPACE = os.environ.get("OCR_COORD_SPACE", "1000")

# read_screen cap: beyond this, detected text (often noisy on a busy
# desktop) inflates the LLM's context for diminishing returns.
READ_SCREEN_MAX_ELEMENTS = int(os.environ.get("OCR_READ_SCREEN_MAX_ELEMENTS", "80"))

engine = get_engine()

mcp = FastMCP("ocr-service")


async def _capture_screen() -> tuple[bytes, int, int]:
    headers = {"Authorization": f"Bearer {GHOSTDESK_AUTH_TOKEN}"}
    async with streamablehttp_client(GHOSTDESK_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("screen_shot", {"format": "png"})

    image_block = next(block for block in result.content if getattr(block, "type", None) == "image")
    image_bytes = base64.b64decode(image_block.data)
    with Image.open(io.BytesIO(image_bytes)) as image:
        width, height = image.size
    return image_bytes, width, height


async def _detect_all() -> list[dict]:
    image_bytes, width, height = await _capture_screen()
    raw_detections = engine.run(image_bytes)
    return [convert_detection(d, width, height, OCR_COORD_SPACE) for d in raw_detections]


def _public_fields(detection: dict) -> dict:
    return {key: detection[key] for key in ("text", "x", "y", "width", "height", "confidence")}


@mcp.tool()
async def find_text(query: str, fuzzy: bool = True) -> list[dict]:
    """Localise un texte visible à l'écran (capture fraîche + OCR) et
    retourne ses coordonnées exactes, directement utilisables par
    mouse_click. À utiliser de préférence à l'estimation visuelle des
    coordonnées pour cliquer sur un élément textuel. Correspondances triées
    par confiance décroissante ; liste vide si rien ne correspond (jamais
    d'erreur). fuzzy=True (défaut) tolère les erreurs de lecture ponctuelles
    de l'OCR."""
    detections = await _detect_all()
    matched = [d for d in detections if matches(query, d["text"], fuzzy)]
    matched.sort(key=lambda d: d["confidence"], reverse=True)
    return [_public_fields(d) for d in matched]


@mcp.tool()
async def read_screen() -> list[dict]:
    """Retourne tout le texte détecté à l'écran (capture fraîche + OCR) avec
    ses coordonnées, jusqu'à 80 éléments triés par confiance décroissante."""
    detections = await _detect_all()
    detections.sort(key=lambda d: d["confidence"], reverse=True)
    return [_public_fields(d) for d in detections[:READ_SCREEN_MAX_ELEMENTS]]


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


class RequireBearerToken:
    """Only requires the bearer token on MCP routes — /health stays
    accessible with no authentication, for the docker-compose healthcheck."""

    def __init__(self, asgi_app: ASGIApp, token: str):
        self.asgi_app = asgi_app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == "/health":
            await self.asgi_app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        auth = headers.get(b"authorization", b"").decode()
        if auth != f"Bearer {self.token}":
            response = PlainTextResponse("token invalide", status_code=401)
            await response(scope, receive, send)
            return
        await self.asgi_app(scope, receive, send)


app = RequireBearerToken(mcp.streamable_http_app(), OCR_AUTH_TOKEN)
