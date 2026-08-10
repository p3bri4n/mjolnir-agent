"""
Tests of POST /ocr against the real FastAPI app (TestClient, same
pattern as context-manager/skill-manager): no network round trip, no
subprocess — OCR_ENGINE=fake (tests/conftest.py) makes
FakeOCREngine.run() return whatever set_fake_detections() set,
regardless of the image bytes actually posted.
"""

import base64

import pytest
from fastapi.testclient import TestClient

from app.ocr_engine import set_fake_detections

# Any base64-valid payload works: FakeOCREngine.run() ignores its input.
FAKE_IMAGE_B64 = base64.b64encode(b"not a real image").decode()


@pytest.fixture
def client():
    import app.main as main_mod

    return TestClient(main_mod.app)


def _detection(text, confidence):
    return {"text": text, "x": 0, "y": 0, "width": 0, "height": 0, "confidence": confidence}


def test_ocr_returns_matches_sorted_by_confidence(client):
    set_fake_detections(
        [
            _detection("Fichier", confidence=0.7),
            _detection("Fichiers récents", confidence=0.95),
            _detection("Édition", confidence=0.9),
        ]
    )

    resp = client.post("/ocr", json={"image_base64": FAKE_IMAGE_B64})

    assert resp.status_code == 200
    result = resp.json()
    assert [d["text"] for d in result] == ["Fichiers récents", "Édition", "Fichier"]
    assert result[0]["confidence"] == 0.95


def test_ocr_returns_only_text_and_confidence(client):
    set_fake_detections([_detection("Fichier", confidence=0.9)])

    resp = client.post("/ocr", json={"image_base64": FAKE_IMAGE_B64})

    assert resp.json() == [{"text": "Fichier", "confidence": 0.9}]


def test_ocr_no_detections_returns_empty_list(client):
    set_fake_detections([])

    resp = client.post("/ocr", json={"image_base64": FAKE_IMAGE_B64})

    assert resp.status_code == 200
    assert resp.json() == []


def test_ocr_caps_at_80_elements_sorted_by_confidence(client):
    detections = [_detection(f"mot{i}", confidence=round(i / 100, 3)) for i in range(90)]
    set_fake_detections(detections)

    resp = client.post("/ocr", json={"image_base64": FAKE_IMAGE_B64})

    result = resp.json()
    assert len(result) == 80
    # Les 80 plus hautes confiances (0.10 à 0.89), triées décroissant.
    assert result[0]["confidence"] == 0.89
    assert result[-1]["confidence"] == round(10 / 100, 3)


def test_ocr_default_mime_type_is_png(client):
    """mime_type is accepted but unused server-side (engine.run() takes raw
    bytes only) — this just confirms the request model doesn't require it."""
    set_fake_detections([_detection("x", confidence=0.5)])

    resp = client.post("/ocr", json={"image_base64": FAKE_IMAGE_B64})

    assert resp.status_code == 200


def test_health() -> None:
    import app.main as main_mod

    client = TestClient(main_mod.app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
