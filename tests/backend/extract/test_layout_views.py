import json

import pytest

from backend.extract import layout_context
from backend.extract.layout_views import LOCATOR_USER_PROMPT, VERSION, plan_regions


SIZE = (1000, 800)


def box(x0, y0, x1, y1):
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1}


def region(kind, bounds, context=None, **details):
    return {"region_type": kind, "bbox": bounds, "context_bbox": context or bounds,
            "classification_status": "supported", "boundary_status": "complete", **details}


def test_locator_example_is_valid_json_and_plan_keeps_unknowns():
    example, _ = json.JSONDecoder().raw_decode(LOCATOR_USER_PROMPT[LOCATOR_USER_PROMPT.index("{"):])
    plan = plan_regions(example, SIZE)
    assert plan["version"] == VERSION == "layout-context-v2"
    assert plan["coverage"] == "unknown" and plan["whole_complete"] is False
    view, uncertain = plan["regions"]
    assert view["state"] == "planned"
    assert uncertain["state"] == uncertain["classification_status"] == "classification_uncertain"
    assert uncertain["boundary_status"] == "unknown"
    assert uncertain["classification_reason"] == "无法辨认栏内字段"
    assert plan["unlocated"] == [{"region_type": "other", "reason": "图面内容重叠，无法确定独立边界", "state": "unknown"}]


def test_view_crop_contains_subject_horizontal_and_vertical_dimensions_and_label():
    # Synthetic normalized boxes for a subject, two outer chains, and its view label.
    subject = box(.32, .36, .53, .57)
    horizontal = box(.31, .29, .54, .34)
    vertical = box(.24, .35, .29, .58)
    label = box(.32, .59, .53, .62)
    view = region("drawing_view", subject, box(.2341, .28, .5478, .623))
    result, = plan_regions({"regions": [view]}, SIZE)["regions"]
    assert result["bbox"] == subject
    left, top, right, bottom = result["pixel_box"]
    assert result["pixel_box"] == [234, 224, 548, 499]
    for part in (subject, horizontal, vertical, label):
        assert left <= part["x0"]*SIZE[0] < part["x1"]*SIZE[0] <= right
        assert top <= part["y0"]*SIZE[1] < part["y1"]*SIZE[1] <= bottom

    with pytest.raises(layout_context.LayoutContextError, match="CONTEXT_EXCLUDES_REGION"):
        plan_regions({"regions": [region("drawing_view", subject, horizontal)]}, SIZE)


def test_stable_order_and_uncertain_region_does_not_consume_budget():
    regions = [region("notes", box(.31, .51, .55, .61)),
               region("notes", box(.29, .19, .51, .4)),
               region("table", box(.62, .26, .81, .43), boundary_status="partial",
                      classification_status="classification_uncertain", classification_reason="无法辨认数据行"),
               region("drawing_view", box(.14, .45, .42, .63))]
    plan = plan_regions({"regions": regions}, SIZE, max_regions=2)
    assert plan == plan_regions({"regions": regions[::-1]}, SIZE, max_regions=2)
    assert [(row["region_type"], row["state"]) for row in plan["regions"]] == [
        ("notes", "planned"), ("table", "classification_uncertain"),
        ("drawing_view", "planned"), ("notes", "not_scheduled")]
    assert plan["regions"][1]["boundary_status"] == "partial"


def test_uncertainty_reason_required_and_coverage_claim_does_not_promote_plan():
    unknown = region("title", box(.3, .5, .6, .7), classification_status="classification_uncertain")
    with pytest.raises(layout_context.LayoutContextError, match="EXPECTED_TEXT.*classification_reason"):
        plan_regions({"regions": [unknown]}, SIZE)
    plan = plan_regions({"regions": [], "whole_complete": True, "coverage": "complete"}, SIZE)
    assert plan["coverage"] == "unknown" and plan["whole_complete"] is False


def test_v1_type_contract_still_replays_an_old_shape():
    old = {"region_type": "dimension_chain", "bbox": box(.4, .2, .5, .3),
           "context_bbox": box(.3, .1, .6, .4), "boundary_status": "complete"}
    plan = layout_context.plan_regions({"regions": [old]}, SIZE)
    assert plan["version"] == "layout-context-v1"
    assert plan["regions"] == [{**old, "pixel_box": [300, 80, 600, 320], "id": "region-1", "state": "planned"}]
    with pytest.raises(layout_context.LayoutContextError, match="INVALID_ENUM.*region_type"):
        plan_regions({"regions": [old]}, SIZE)


def test_note_alias_retains_source_type_and_preserves_input():
    note = region("note", box(.4, .2, .5, .3))
    missing = {"region_type": "note", "reason": "段落位置无法辨认"}
    payload = {"regions": [note], "unlocated": [missing]}
    plan = plan_regions(payload, SIZE)
    assert note["region_type"] == missing["region_type"] == "note"
    assert plan["regions"][0]["region_type"] == plan["unlocated"][0]["region_type"] == "notes"
    assert plan["regions"][0]["source_region_type"] == plan["unlocated"][0]["source_region_type"] == "note"
    with pytest.raises(layout_context.LayoutContextError, match="INVALID_ENUM.*region_type"):
        plan_regions({"regions": [{**note, "region_type": "NOTE"}]}, SIZE)
