from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from backend.extract.layout_context import LayoutContextError
from backend.extract.line_regions import bind_note_lines, locate_note_lines


def sample():
    image = Image.new("RGB", (200, 120), "white")
    pen = ImageDraw.Draw(image)
    # Two repeated text-like bands must retain separate physical identities.
    for top in (12, 62):
        for x in range(20, 180, 12):
            pen.rectangle((x, top, x + 5, top + 9), fill="black")
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def test_rows_are_physical_image_bands_not_model_positions():
    bands = locate_note_lines(sample())
    assert [r["pixel_box"] for r in bands["lines"]] == [[0, 12, 200, 22], [0, 62, 200, 72]]
    payload = {"context_status": "complete", "lines": [
        {"id": "l1", "text": "重复读数", "paragraph_id": "p1"},
        {"id": "l2", "text": "重复读数", "paragraph_id": "p2"}], "measurements": []}
    result = bind_note_lines(payload, bands)
    assert result["lines"][0]["bbox"] != result["lines"][1]["bbox"]
    assert "bbox" not in payload["lines"][0]
    assert bands["quality_status"] == "unassessed"
    assert bands["coverage"] == "unknown"


def test_missing_row_and_invented_geometry_cannot_be_silently_accepted():
    bands = locate_note_lines(sample())
    with pytest.raises(LayoutContextError, match="MISSING_INK_BANDS"):
        bind_note_lines({"lines": [{"id": "l1", "text": "one"}]}, bands)
    rows = [{"id": key, "text": "same"} for key in ("l1", "l2")]
    with pytest.raises(LayoutContextError, match="MODEL_GEOMETRY_NOT_ALLOWED"):
        bind_note_lines({"lines": rows, "measurements": [{"literal": {"bbox": {}}}]}, bands)
    rows[0]["bbox"] = {}
    with pytest.raises(LayoutContextError, match="MODEL_GEOMETRY_NOT_ALLOWED"):
        bind_note_lines({"lines": rows}, bands)


def test_blank_image_has_no_invented_line_and_retains_unknown_coverage():
    out = BytesIO()
    Image.new("RGB", (100, 100), "white").save(out, format="PNG")
    plan = locate_note_lines(out.getvalue())
    assert plan["lines"] == []
    assert plan["coverage"] == "unknown"


def test_unread_row_is_not_measurement_evidence_or_complete_context():
    bands = locate_note_lines(sample())
    rows = [{"id": "l1", "text": "未读清"}, {"id": "l2", "text": "visible"}]
    assert bind_note_lines({"lines": rows, "context_status": "complete"}, bands)["context_status"] == "partial"
    with pytest.raises(LayoutContextError, match="UNREAD_LINE_REFERENCE"):
        bind_note_lines({"lines": rows, "measurements": [{"literal": {"line_id": "l1", "quote": "未读清"}}]}, bands)
    with pytest.raises(LayoutContextError, match="FIXED_LINE_ORDER"):
        bind_note_lines({"lines": rows[::-1]}, bands)
