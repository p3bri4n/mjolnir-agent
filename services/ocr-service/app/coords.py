"""
Converts OCR coordinates (real capture pixels) to the normalized 0-1000
coordinate space expected by mouse_click on the GhostDesk side (see
GHOSTDESK_MODEL_SPACE in mcp-client, the same space used by Qwen models)
— a classic source of misplaced clicks if forgotten: GhostDesk by
default interprets received coordinates as native screen pixels, while
the LLM reasons (and will therefore click) in the 0-1000 space.

OCR_COORD_SPACE=pixels disables this conversion (raw OCR coordinates),
useful if the service calling mouse_click itself works in pixels.
"""

COORD_SPACE_NORMALIZED = "1000"
COORD_SPACE_PIXELS = "pixels"


def _to_normalized(value_px: float, dimension_px: int) -> int:
    return round(value_px * 1000 / dimension_px)


def convert_detection(detection: dict, image_width: int, image_height: int, coord_space: str) -> dict:
    if coord_space == COORD_SPACE_PIXELS:
        return detection

    return {
        **detection,
        "x": _to_normalized(detection["x"], image_width),
        "y": _to_normalized(detection["y"], image_height),
        "width": _to_normalized(detection["width"], image_width),
        "height": _to_normalized(detection["height"], image_height),
    }
