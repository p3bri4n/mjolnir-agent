"""
OCR engine: PaddleOCR on CPU (both GPUs are already saturated by
llama-server, see README) for find_text/read_screen.

fr + en languages: PaddleOCR groups French and English (Latin alphabet)
under a single recognition model ("lang=fr" already covers English in
practice, both share the same Latin character set) — no need to run two
separate OCR passes for this project. OCR_LANGS stays configurable if a
deployment needs a different alphabet.

In test environments, OCR_ENGINE=fake switches to a deterministic engine
with no PaddleOCR dependency (same principle as context-manager's
EMBEDDING_MODEL=fake): the returned detections are injected by the test
via set_fake_detections(), never computed from the received image.
"""

import os

OCR_ENGINE_NAME = os.environ.get("OCR_ENGINE", "paddleocr")
OCR_LANGS = os.environ.get("OCR_LANGS", "fr")

_fake_detections: list[dict] = []


def set_fake_detections(detections: list[dict]) -> None:
    """Reserved for tests (OCR_ENGINE=fake): controls what FakeOCREngine.run() returns."""
    global _fake_detections
    _fake_detections = detections


class FakeOCREngine:
    def run(self, image_bytes: bytes) -> list[dict]:
        return [dict(detection) for detection in _fake_detections]


class PaddleOCREngine:
    def __init__(self, lang: str):
        # lazy import: only deployments that don't set OCR_ENGINE=fake
        # need paddleocr/paddlepaddle, heavy dependencies absent from the
        # test environment.
        from paddleocr import PaddleOCR

        self._ocr = PaddleOCR(use_angle_cls=False, lang=lang, show_log=False)

    def run(self, image_bytes: bytes) -> list[dict]:
        import io

        import numpy as np
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as image:
            array = np.array(image.convert("RGB"))

        result = self._ocr.ocr(array, cls=False)
        lines = result[0] if result else []

        detections = []
        for box, (text, confidence) in lines or []:
            xs = [point[0] for point in box]
            ys = [point[1] for point in box]
            x, y = min(xs), min(ys)
            detections.append(
                {
                    "text": text,
                    "x": x,
                    "y": y,
                    "width": max(xs) - x,
                    "height": max(ys) - y,
                    "confidence": float(confidence),
                }
            )
        return detections


def get_engine():
    if OCR_ENGINE_NAME == "fake":
        return FakeOCREngine()
    return PaddleOCREngine(lang=OCR_LANGS)
