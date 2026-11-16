from copy import deepcopy
from io import BytesIO

from PIL import Image, ImageDraw
import pytest

from backend.extract.layout_boundary import propose_boundaries
from backend.extract.layout_views import plan_regions
from backend.extract.layout_caption import propose_underlined_captions


SIZE = (600, 400)


def region(kind, bbox):
    x0, y0, x1, y1 = bbox
    box = {"x0": x0, "y0": y0, "x1": x1, "y1": y1}
    return {"region_type": kind, "bbox": box, "context_bbox": box,
            "classification_status": "supported", "boundary_status": "complete"}


def run_candidate(*, second_underline=True, other_ink=False, neighbors=()):
    page = Image.new("L", SIZE, 255)
    draw = ImageDraw.Draw(page)
    draw.rectangle((220, 97, 380, 170), outline=0)
    for x in range(260, 335, 12):
        draw.rectangle((x, 202, x + 6, 211), fill=0)
    draw.line((245, 217, 355, 217), fill=0)
    if second_underline:
        draw.line((245, 222, 355, 222), fill=0)
    if other_ink:
        draw.rectangle((140, 196, 155, 205), fill=0)
    buffer = BytesIO()
    page.save(buffer, format="PNG")
    png = buffer.getvalue()
    plan = plan_regions({"regions": [region("drawing_view", (.2, .2, .8, .45)), *neighbors]}, SIZE)
    base = propose_boundaries(png, plan)
    original = deepcopy(base)
    candidate = propose_underlined_captions(png, plan, base)
    assert base == original
    assert candidate["coverage"] == "unknown" and candidate["whole_complete"] is False
    return candidate["regions"][0]


def test_only_two_matching_rules_and_a_detached_text_band_add_caption_context():
    result = run_candidate()
    assert result["status"] == "structural_caption_candidate" and result["boundary_status"] == "unknown"
    assert result["base_candidate_pixel_box"] == [120, 80, 480, 180]
    assert result["proposed_context_pixel_box"] == [120, 80, 480, 225]
    assert result["caption_options"][0]["underline_pixel_boxes"] == [[245, 217, 356, 218], [245, 222, 356, 223]]
    assert run_candidate(second_underline=False)["proposed_context_pixel_box"] is None


@pytest.mark.parametrize("case", ["other_ink", "neighbor"])
def test_caption_extension_is_held_for_competing_ink_or_neighboring_region(case):
    options = {"other_ink": True} if case == "other_ink" else {
        "neighbors": [region("title", (.47, .51, .6, .58))]}
    result = run_candidate(**options)
    assert result["status"] == "unknown" and result["proposed_context_pixel_box"] is None
    option, = result["caption_options"]
    assert option["other_ink_in_added_crop"] or option["blocking_neighbor_ids"]
