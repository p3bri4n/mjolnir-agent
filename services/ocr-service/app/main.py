"""
OCR Service: a graph-internal capability, called directly by
langgraph-agent over plain HTTP — not an MCP server, no model-facing
tool, no GhostDesk dependency. Complements the VLM (Qwen3.6 MoE), which
reasons well but localizes poorly (imprecise grounding from a
general-purpose vision model with no dedicated UI-element detection).

The caller supplies the image (base64 + MIME type, matching the shape of
an MCP image content block, e.g. from Playwright's
`browser_take_screenshot`) — this service never captures anything
itself. One endpoint: POST /ocr, returns detected text sorted by
descending confidence, capped at OCR_MAX_ELEMENTS, never an error (empty
list if nothing found).
"""

import base64
import os

from fastapi import FastAPI
from pydantic import BaseModel

from app.ocr_engine import get_engine

# Detected text beyond this count (often noisy on a busy page) inflates
# the LLM's context for diminishing returns.
OCR_MAX_ELEMENTS = int(os.environ.get("OCR_MAX_ELEMENTS", "80"))

engine = get_engine()

app = FastAPI(title="OCR Service")


class OCRRequest(BaseModel):
    image_base64: str
    mime_type: str = "image/png"


@app.post("/ocr")
async def ocr(request: OCRRequest) -> list[dict]:
    image_bytes = base64.b64decode(request.image_base64)
    detections = engine.run(image_bytes)
    detections.sort(key=lambda d: d["confidence"], reverse=True)
    return [{"text": d["text"], "confidence": d["confidence"]} for d in detections[:OCR_MAX_ELEMENTS]]


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
