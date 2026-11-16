from copy import deepcopy
from io import BytesIO

from PIL import Image, ImageDraw
import pytest

from backend.extract.layout_views import plan_regions
from backend.extract.layout_boundary import propose_boundaries
from backend.extract.note_boundary import propose_note_crop


SIZE = (500, 300)


def box(left, top, right, bottom):
    return {"x0": left / SIZE[0], "y0": top / SIZE[1], "x1": right / SIZE[0], "y1": bottom / SIZE[1]}


def region(kind, primary, context):
    return {"region_type": kind, "bbox": box(*primary), "context_bbox": box(*context),
            "classification_status": "supported", "boundary_status": "complete"}


def png_with_ink(boxes):
    page = Image.new("L", SIZE, 255)
    draw = ImageDraw.Draw(page)
    for ink_box in boxes:
        draw.rectangle(ink_box, fill=0)
    out = BytesIO()
    page.save(out, format="PNG")
    return out.getvalue()


def glyphs(positions, y0, y1):
    return [(x, y0, x + 8, y1) for x in positions]


def test_notes_follow_each_line_across_model_right_edge():
    plan = plan_regions({"regions": [region("notes", (250, 100, 370, 160), (240, 95, 385, 170))],
                         "unlocated": [{"region_type": "other", "reason": "uncertain block"}]}, SIZE)
    old = deepcopy(plan)
    ink = glyphs(range(266, 413, 14), 105, 116) + glyphs(range(266, 347, 14), 135, 146)
    png = png_with_ink(ink + [(440, 105, 451, 116)])
    proposal = propose_boundaries(png, plan)
    note, = proposal["regions"]
    assert proposal == propose_boundaries(png, plan)
    assert plan == old
    assert proposal["locator_unlocated"] == plan["unlocated"]
    assert proposal["coverage"] == "unknown" and proposal["whole_complete"] is False
    assert note["model_pixel_box"] == [240, 95, 385, 170]
    left, top, right, bottom = note["candidate_pixel_box"]
    assert (left, top, bottom) == (240, 95, 170)
    assert 415 <= right < 440
    assert note["edge_checks"]["right"]["bands"][0]["stop"] == "separating_whitespace"
    assert note["edge_checks"]["bottom"]["stop"] == "vertical_notes_boundary_unassessed"
    assert note["boundary_status"] == "unknown" and note["quality_status"] == "unassessed"


def test_view_joins_caption_across_white_edge_and_blocks_neighbor_notes():
    view = region("drawing_view", (55, 53, 147, 106), (45, 40, 155, 124))
    notes = region("notes", (165, 64, 240, 94), (158, 48, 250, 100))
    plan = plan_regions({"regions": [view, notes]}, SIZE)
    png = png_with_ink([(55, 53, 145, 104), (77, 131, 133, 141), (66, 157, 146, 167),
                        (165, 50, 208, 58), (165, 65, 235, 79), (60, 223, 145, 233),
                        (10, 7, 10, 295), (10, 7, 488, 7)])
    proposal = propose_boundaries(png, plan)
    _, result = proposal["regions"]
    assert proposal["parameters"]["frame_rows_suppressed"] == 1
    assert proposal["parameters"]["frame_columns_suppressed"] == 1
    left, _, right, bottom = result["candidate_pixel_box"]
    assert left > 10 and right <= 165
    assert 168 <= bottom < 223
    assert [join["joined_ink"] for join in result["edge_checks"]["bottom"]["joined"]] == [[131, 142], [157, 168]]
    assert result["neighbor_blocks"][0]["basis"] == "note_anchor_columns_in_context_rows"
    assert "view_association_unverified" in result["uncertainty_reasons"]


def test_rectangle_that_would_add_neighbor_ink_is_held_at_original_box():
    notes = region("notes", (250, 100, 370, 160), (240, 95, 385, 170))
    title = region("title", (389, 125, 418, 153), (389, 125, 418, 153))
    plan = plan_regions({"regions": [notes, title]}, SIZE)
    png = png_with_ink(glyphs(range(266, 413, 14), 105, 116) + [(392, 130, 410, 144)])
    result = propose_boundaries(png, plan)["regions"][0]
    assert result["candidate_pixel_box"] == result["model_pixel_box"]
    assert result["rejected_candidate_pixel_box"][2] >= 415
    assert "would_include_neighbor_ink" in result["uncertainty_reasons"]
    assert result["boundary_status"] == "unknown"


def test_image_size_must_match_frozen_plan():
    plan = plan_regions({"regions": []}, (300, 300))
    with pytest.raises(ValueError, match="source size"):
        propose_boundaries(png_with_ink([]), plan)


def test_note_boundary_preserves_a_clipped_continuation_and_checks_its_right_edge():
    notes = region("notes", (250, 100, 370, 138), (240, 95, 385, 145))
    plan = plan_regions({"regions": [notes]}, SIZE)
    notes, = plan["regions"]
    png = png_with_ink(glyphs(range(260, 350, 14), 105, 115) +
                       glyphs(range(260, 350, 14), 122, 132) +
                       glyphs(range(260, 415, 14), 140, 150) + [(260, 210, 340, 220)])
    proposal = propose_note_crop(png, notes, plan["regions"])
    assert proposal["source_pixel_box"] == [240, 95, 385, 145]
    _, _, right, bottom = proposal["candidate_pixel_box"]
    assert 423 <= right and 151 <= bottom < 210
    assert proposal["additional_candidate_ink_px"] > 0
    assert proposal["boundary_status"] == "unknown" and not proposal["whole_complete"]


def test_note_boundary_rejects_continuation_candidate_that_would_add_neighbor_ink():
    notes = region("notes", (250, 100, 370, 138), (240, 95, 385, 145))
    neighbor = region("table", (388, 115, 415, 135), (388, 115, 415, 135))
    plan = plan_regions({"regions": [notes, neighbor]}, SIZE)
    png = png_with_ink(glyphs(range(260, 350, 14), 105, 115) +
                       glyphs(range(260, 350, 14), 122, 132) +
                       glyphs(range(260, 415, 14), 140, 150) + [(392, 120, 412, 131)])
    proposal = propose_note_crop(png, plan["regions"][0], plan["regions"])
    assert proposal["neighbor_blocks"][0]["region_id"] == plan["regions"][1]["id"]
    assert proposal["rejected_candidate_pixel_box"] is not None
    assert proposal["candidate_pixel_box"] == proposal["source_pixel_box"]
    assert proposal["boundary_status"] == "unknown"
