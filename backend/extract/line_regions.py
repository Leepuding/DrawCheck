"""Deterministic ink bands for notes-only experiments, never OCR truth."""
from __future__ import annotations

from hashlib import sha256
from io import BytesIO

from PIL import Image


VERSION = "notes-ink-bands-v1"


def locate_note_lines(png: bytes) -> dict:
    """Freeze visible horizontal ink bands without guessing text or semantics.

    This is deliberately restricted to a preselected notes crop. A band may
    contain unrelated marks; no band is asserted to be a sentence or object.
    Coordinates cover the entire crop width, so precision is row, not word.
    """
    with Image.open(BytesIO(png)) as source:
        gray = source.convert("L")
    width, height = gray.size
    ink = gray.point(lambda value: 255 if value < 180 else 0)
    minimum_ink = max(3, round(width * .0075))
    active = [y for y in range(height)
              if ink.crop((0, y, width, y + 1)).histogram()[255] >= minimum_ink]
    groups = []
    for y in active:
        if not groups or y - groups[-1][-1] > 2:
            groups.append([y])
        else:
            groups[-1].append(y)
    lines, rejected = [], []
    for group in groups:
        top, bottom = group[0], group[-1] + 1
        if bottom - top < 3:
            rejected.append({"y0": top, "y1": bottom, "reason": "thin_ink_band"})
            continue
        side_ink = bool(ink.crop((0, top, 1, bottom)).getbbox() or
                        ink.crop((width - 1, top, width, bottom)).getbbox())
        lines.append({"id": f"l{len(lines)+1}",
                      "pixel_box": [0, top, width, bottom],
                      "bbox": {"x0": 0, "y0": top / height, "x1": 1, "y1": bottom / height},
                      "touches_crop_edge": top == 0 or bottom == height or side_ink,
                      "edge_check_basis": "visible_ink_at_crop_edge_not_full_width_row_box",
                      "geometry_source": VERSION, "geometry_precision": "row",
                      "text_status": "unread"})
    return {"version": VERSION, "image_sha256": sha256(png).hexdigest(),
            "image_size": [width, height], "lines": lines, "rejected_bands": rejected,
            "coverage": "unknown", "quality_status": "unassessed"}


def bind_note_lines(payload: dict, bands: dict) -> dict:
    """Bind model text to frozen IDs; never accept model coordinates instead.

    Missing or unread rows cannot silently disappear. Paragraph IDs remain
    model candidates, and are subject to independent source-image review.
    """
    from copy import deepcopy
    from backend.extract.layout_context import LayoutContextError

    def require(condition, code, location):
        if not condition:
            raise LayoutContextError(code, location)

    require(isinstance(payload, dict), "EXPECTED_OBJECT", "region_response")
    rows = payload.get("lines")
    require(isinstance(rows, list), "EXPECTED_ARRAY", "lines")
    by_id = {row["id"]: row for row in bands["lines"]}
    found = set()
    unread = set()
    output = deepcopy(payload)
    for index, row in enumerate(output["lines"]):
        require(isinstance(row, dict), "EXPECTED_OBJECT", f"lines[{index}]")
        key = row.get("id")
        require(isinstance(key, str) and key in by_id and key not in found,
                "UNKNOWN_OR_REPEATED_LINE", f"lines[{index}].id")
        require(index < len(bands["lines"]) and key == bands["lines"][index]["id"],
                "FIXED_LINE_ORDER", f"lines[{index}].id")
        require("bbox" not in row, "MODEL_GEOMETRY_NOT_ALLOWED", f"lines[{index}].bbox")
        if row.get("text") == "未读清":
            unread.add(key)
        row["bbox"] = dict(by_id[key]["bbox"])
        found.add(key)
    require(found == by_id.keys(), "MISSING_INK_BANDS", "lines")
    # Reject word boxes too: no model coordinate can acquire host provenance.
    def reject_nested_boxes(value, path):
        if isinstance(value, dict):
            require("bbox" not in value, "MODEL_GEOMETRY_NOT_ALLOWED", path)
            require(value.get("line_id") not in unread, "UNREAD_LINE_REFERENCE", path)
            for key, item in value.items():
                reject_nested_boxes(item, f"{path}.{key}")
        elif isinstance(value, list):
            for i, item in enumerate(value):
                reject_nested_boxes(item, f"{path}[{i}]")
    reject_nested_boxes(payload.get("measurements", []), "measurements")
    if unread or any(row["touches_crop_edge"] for row in bands["lines"]) or bands["rejected_bands"]:
        output["context_status"] = "partial"
    return output
