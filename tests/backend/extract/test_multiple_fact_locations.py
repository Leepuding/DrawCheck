"""Repeated source labels must retain each supplied location, never a whole-page guess."""
import copy

import pytest

from backend.extract.errors import ExtractError
from backend.extract.schema import validate_extract_payload
from backend.extract.service import ExtractService


BOXES = [
    {"x0": .282, "y0": .573, "x1": .312, "y1": .584},
    {"x0": .662, "y0": .573, "x1": .692, "y1": .584},
]


def payload(**extra):
    return {"title_blocks": [], "facts": [{"text": "14.000", "category": "尺寸",
        "numeric_value": 14.0, "unit": "m", "bbox": copy.deepcopy(BOXES),
        "confidence": .9, "unclear": False, "identity_confirmed": False, **extra}]}


def test_repeated_label_preserves_both_actual_locations_without_changing_claim():
    source = payload()
    normalized = validate_extract_payload(source)
    normalized, notes = ExtractService._clamp_payload(None, normalized)
    assert not notes
    assert [fact["bbox"] for fact in normalized["facts"]] == BOXES
    assert all(fact["text"] == "14.000" and fact["category"] == "尺寸"
               and fact["confidence"] == .9 and not fact["unclear"]
               and not fact["identity_confirmed"] for fact in normalized["facts"])
    assert source["facts"][0]["bbox"] == BOXES
    assert validate_extract_payload(normalized) == normalized


def test_duplicate_location_is_one_physical_reading():
    normalized = validate_extract_payload(payload(bbox=[BOXES[0], BOXES[0]]))
    assert len(normalized["facts"]) == 1


@pytest.mark.parametrize("extra", [
    {"unclear": True}, {"region_conflict": True}, {"bbox_clamped": True},
    {"numeric_value": [14, 15]}, {"unit": ["m", "mm"]},
])
def test_multiple_locations_do_not_repair_other_uncertainty(extra):
    facts = validate_extract_payload(payload(**extra))["facts"]
    assert len(facts) == 2
    assert all(fact["unclear"] for fact in facts)
    if "numeric_value" in extra:
        assert all(fact["numeric_value"] is None and fact["numeric_value_invalid"] for fact in facts)
    if "unit" in extra:
        assert all(fact["unit"] is None and fact["unit_invalid"] for fact in facts)


@pytest.mark.parametrize("boxes", [
    [], [BOXES[0]] * 17, [BOXES[0], None], [BOXES[0], {"x0": .2}],
    [.1, .2, .3, .4], [{**BOXES[0], "x1": float("nan")}],
    [{**BOXES[0], "x1": float("inf")}], [{**BOXES[0], "x0": True}],
    [{**BOXES[0], "x1": 10**400}],
    [{**BOXES[0], "x0": -.1}], [{**BOXES[0], "x1": 1.1}],
    [{**BOXES[0], "x1": BOXES[0]["x0"]}], [{**BOXES[0], "y1": .1}],
])
def test_invalid_multiple_location_never_becomes_a_whole_page_box(boxes):
    with pytest.raises(ExtractError):
        validate_extract_payload(payload(bbox=boxes))
