from copy import deepcopy
import json
from pathlib import Path

import pytest

from backend.extract.layout_context import LayoutContextError
from backend.extract.line_transcription import (
    build_relations_prompt, build_transcribe_prompt, diagnose_relations, validate_relations, validate_transcription,
)
from backend.extract.schema import parse_model_json


IDS = ("l1", "l2")


def test_transcription_keeps_literal_whitespace_glyphs_and_unread_rows():
    raw = {"lines": [{"id": "l1", "text": "  φ2 / 0O ＋  "}, {"id": "l2", "text": None}]}
    assert validate_transcription(raw, IDS) == raw


@pytest.mark.parametrize(("lines", "code"), [
    ([], "TRANSCRIPTION_LINE_COUNT"),
    ([{"id": "l1", "text": ""}, {"id": "l2", "text": None}], "INVALID_TRANSCRIPTION_TEXT"),
    ([{"id": "l1", "text": "甲\n乙"}, {"id": "l2", "text": None}], "INVALID_TRANSCRIPTION_TEXT"),
    ([{"id": "l1", "text": "甲\u2028乙"}, {"id": "l2", "text": None}], "INVALID_TRANSCRIPTION_TEXT"),
    ([{"id": "l2", "text": "甲"}, {"id": "l1", "text": None}], "TRANSCRIPTION_LINE_ID_ORDER"),
    ([{"id": "l1", "text": "甲"}, {"id": "l1", "text": None}], "TRANSCRIPTION_LINE_ID_ORDER"),
    ([{"id": "l1", "text": "甲", "bbox": {}}, {"id": "l2", "text": None}], "INVALID_TRANSCRIPTION_LINE"),
])
def test_transcription_rejects_broken_physical_row_contract(lines, code):
    with pytest.raises(LayoutContextError, match=code):
        validate_transcription({"lines": lines}, IDS)


def test_transcription_rejects_relation_fields():
    with pytest.raises(LayoutContextError, match="INVALID_TRANSCRIPTION_ROOT"):
        validate_transcription({"lines": [], "measurements": None}, IDS)


def test_transcription_prompt_uses_valid_minimal_json_example_and_real_order():
    prompt = build_transcribe_prompt(IDS)
    example, _ = json.JSONDecoder().raw_decode(prompt[prompt.index("{"):])
    assert validate_transcription(example, ["demo-A", "demo-B"]) == example
    assert json.loads(prompt.splitlines()[-1]) == list(IDS)


def _frozen_notes():
    texts = ["页眉说明", "（1）试件甲", "宽度≥12 cm", None, "长度≥24 cm", "（2）另段"]
    lines = [{"id": f"l{index+1}", "text": text} for index, text in enumerate(texts)]
    bands = {"lines": [{"id": line["id"], "touches_crop_edge": False,
                       "bbox": {"x0": 0, "y0": index*.12+.02, "x1": 1, "y1": index*.12+.08}}
                      for index, line in enumerate(lines)], "rejected_bands": []}
    region = {"id": "notes-1", "region_type": "notes", "pixel_box": [100, 200, 900, 600]}
    return {"lines": lines}, bands, region


def _measurement(line_id="l3", prop="宽度", value="12"):
    def quote(key, text):
        return {"line_id": key, "quote": text}

    return {"id": "m1", "literal": quote(line_id, f"{prop}≥{value} cm"),
            "value": quote(line_id, value), "unit": quote(line_id, "cm"),
            "comparator": quote(line_id, "≥"), "property": quote(line_id, prop),
            "object": quote("l2", "试件甲"), "relation_basis": "same_phrase",
            "unit_origin": "explicit", "unit_relation": None}


def test_relations_prompt_uses_host_groups_for_visible_numbering_and_unknown_breaks():
    transcription, _, _ = _frozen_notes()
    prompt = build_relations_prompt(transcription)
    actual = json.loads(prompt.split("以下是实际转录输入。\n", 1)[1])
    assert [row["text"] for row in actual] == [row["text"] for row in transcription["lines"]]
    assert [row["paragraph_id"] for row in actual] == [None, "p-l2", "p-l2", None, None, "p-l6"]
    decimal = {"lines": [{"id": "l1", "text": "1.5 mm"}]}
    actual = json.loads(build_relations_prompt(decimal).split("以下是实际转录输入。\n", 1)[1])
    assert actual[0]["paragraph_id"] is None


def test_relation_result_binds_fixed_geometry_and_keeps_unknown_row_visible():
    transcription, bands, region = _frozen_notes()
    result = validate_relations({"measurements": [_measurement()]}, transcription, bands, region, (1000, 800))
    assert result["unread_line_ids"] == ["l4"]
    assert result["context_status"] == "partial"
    assert result["context_text"].splitlines() == [row["text"] for row in transcription["lines"] if row["text"]]
    reading = result["measurements"][0]
    assert reading["value"]["quote"] == "12"
    assert reading["value"]["bbox_precision"] == "line"
    assert reading["value"]["bbox"] == pytest.approx({"x0": .1, "y0": .38, "x1": .9, "y1": .41})
    assert reading["object_ref"][:2] == ["notes-1", "l2"]
    assert reading["identity_confirmed"] is False


def test_relations_cannot_borrow_subject_across_an_unread_line():
    transcription, bands, region = _frozen_notes()
    with pytest.raises(LayoutContextError, match="UNSUPPORTED_PHRASE_RELATION"):
        validate_relations({"measurements": [_measurement("l5", "长度", "24")]},
                           transcription, bands, region, (1000, 800))


@pytest.mark.parametrize(("marker", "starts_group"), [
    ("2.", True), ("二、", True), ("2、", True), ("2. ", True), ("（2）", True),
    ("２．", True), ("(二)", True), ("（2 ", False), ("2. 1 ", False), ("2.1、", False),
])
def test_numbered_or_unclear_new_line_cannot_lend_its_subject_or_conditions(marker, starts_group):
    transcription, bands, region = _frozen_notes()
    transcription["lines"][3]["text"] = marker + "试件乙仅用于室内"
    actual = validate_relations({"measurements": []}, transcription, bands, region, (1000, 800))
    assert actual["lines"][3]["paragraph_id"] == ("p-l4" if starts_group else None)
    reading = _measurement()
    reading["object"] = {"line_id": "l4", "quote": "试件乙"}
    with pytest.raises(LayoutContextError, match="UNSUPPORTED_PHRASE_RELATION"):
        validate_relations({"measurements": [reading]}, transcription, bands, region, (1000, 800))
    reading = _measurement()
    reading["conditions"] = [{"line_id": "l4", "quote": "仅用于室内"}]
    with pytest.raises(LayoutContextError, match="CONDITION_OUTSIDE_PARAGRAPH"):
        validate_relations({"measurements": [reading]}, transcription, bands, region, (1000, 800))


def test_decimal_measurement_at_start_of_continuation_keeps_its_paragraph():
    transcription, bands, region = _frozen_notes()
    transcription["lines"][3]["text"] = "1.5 mm厚的垫片仍须保留。"
    result = validate_relations({"measurements": [_measurement()]}, transcription, bands, region, (1000, 800))
    assert result["measurements"][0]["context_line_ids"] == ["l2", "l3", "l4", "l5"]


@pytest.mark.parametrize(("boundary", "edge", "expected"), [
    ("complete", False, "complete"),
    ("unknown", False, "unknown"),
    ("partial", False, "partial"),
    (None, False, "unknown"),
    ("complete", True, "partial"),
    ("unknown", True, "partial"),
])
def test_readable_rows_do_not_promote_uncertain_or_cut_parent(boundary, edge, expected):
    transcription, bands, region = _frozen_notes()
    transcription["lines"][3]["text"] = "续行原文"
    if boundary is not None:
        region["boundary_status"] = boundary
    bands["lines"][0]["touches_crop_edge"] = edge
    original_region = deepcopy(region)
    result = validate_relations({"measurements": [_measurement()]}, transcription, bands, region, (1000, 800))
    assert result["context_status"] == expected
    assert result["unread_line_ids"] == []
    assert result["parent_region"] == original_region
    assert region == original_region


@pytest.mark.parametrize(("payload", "code"), [
    ({"measurements": [], "lines": []}, "INVALID_RELATIONS_ROOT"),
    ({"measurements": None}, "EXPECTED_ARRAY"),
    ({"measurements": [_measurement(value="21")]}, "QUOTE_NOT_FOUND"),
    ({"measurements": [{**_measurement(), "value": {"line_id": "l3", "quote": "12", "bbox": {}}}]},
     "MODEL_GEOMETRY_NOT_ALLOWED"),
])
def test_relation_payload_cannot_replace_frozen_source_or_geometry(payload, code):
    transcription, bands, region = _frozen_notes()
    with pytest.raises(LayoutContextError, match=code):
        validate_relations(payload, transcription, bands, region, (1000, 800))


def test_same_line_repeated_object_and_property_resolve_only_in_unique_literal():
    transcription, bands, region = _frozen_notes()
    text = "试件甲宽度待核，试件甲宽度≥12 cm"
    transcription["lines"][2]["text"] = text
    measurement = _measurement()
    measurement["literal"]["quote"] = "试件甲宽度≥12 cm"
    measurement["object"] = {"line_id": "l3", "quote": "试件甲"}
    payload = {"measurements": [measurement]}
    result = validate_relations(payload, transcription, bands, region, (1000, 800))["measurements"][0]
    assert result["object"]["start"] == text.rindex("试件甲")
    assert result["property"]["start"] == text.rindex("宽度")
    measurement["literal"]["quote"] = text
    with pytest.raises(LayoutContextError, match="AMBIGUOUS_QUOTE"):
        validate_relations(payload, transcription, bands, region, (1000, 800))


@pytest.mark.parametrize(("literal", "comparator", "error"), [
    ("宽度至少12 cm", "至少", "INVALID_COMPARATOR"),
    ("宽度>=12 cm", "=", "COMPARATOR_TOKEN_TRUNCATED"),
    ("宽度>=12 cm", ">=", None),
])
def test_comparator_must_be_a_complete_visible_symbol(literal, comparator, error):
    transcription, bands, region = _frozen_notes()
    transcription["lines"][2]["text"] = literal
    measurement = _measurement()
    measurement["literal"]["quote"] = literal
    measurement["comparator"]["quote"] = comparator
    payload = {"measurements": [measurement]}
    if error:
        with pytest.raises(LayoutContextError, match=error):
            validate_relations(payload, transcription, bands, region, (1000, 800))
    else:
        assert validate_relations(payload, transcription, bands, region, (1000, 800))["measurements"][0]["comparator"]["quote"] == comparator


def test_conditions_keep_multi_line_literal_context_separate_from_the_measurement():
    transcription, bands, region = _frozen_notes()
    transcription["lines"][3]["text"] = "若采用替代件，"
    transcription["lines"][4]["text"] = "则先复检。"
    measurement = _measurement()
    measurement["conditions"] = [{"line_id": "l4", "quote": "若采用替代件，"},
                                 {"line_id": "l5", "quote": "则先复检。"}]
    result = validate_relations({"measurements": [measurement]}, transcription, bands, region, (1000, 800))
    reading, = result["measurements"]
    assert [span["quote"] for span in reading["conditions"]] == ["若采用替代件，", "则先复检。"]
    assert all(span["bbox_precision"] == "line" for span in reading["conditions"])
    assert reading["condition_status"] == "candidate"
    assert reading["context_line_ids"] == ["l2", "l3", "l4", "l5"]
    assert reading["comparator"]["quote"] == "≥"


def test_unverified_host_boundary_preserves_locator_claim_only_as_provenance():
    transcription, bands, region = _frozen_notes()
    transcription["lines"][3]["text"] = "续行原文"
    region["boundary_status"] = "complete"
    result = validate_relations({"measurements": [_measurement()]}, transcription, bands, region, (1000, 800),
                                source_boundary_status="unknown")
    assert result["parent_region"]["boundary_status"] == "complete"
    assert result["context_status"] == "unknown"


@pytest.mark.parametrize(("line", "quote", "code"), [
    ("l5", "长度", "CONDITION_OUTSIDE_PARAGRAPH"),
    ("l3", "≥12", "OVERLAPPING_CONDITION"),
    ("l2", "原文没有", "QUOTE_NOT_FOUND"),
])
def test_conditions_refuse_unseen_overlapping_or_disconnected_text(line, quote, code):
    transcription, bands, region = _frozen_notes()
    measurement = _measurement()
    measurement["conditions"] = [{"line_id": line, "quote": quote}]
    payload = {"measurements": [measurement]}
    with pytest.raises(LayoutContextError, match=code):
        validate_relations(payload, transcription, bands, region, (1000, 800))
    assert diagnose_relations(payload, transcription, bands, region, (1000, 800))[0]["code"] == code


def test_saved_live_response_stays_rejected_with_all_independent_quote_errors():
    evidence = Path(__file__).resolve().parents[3] / "_evidence/layout-context-20261106"
    experiment = evidence / "experiment"
    failure = experiment / "final-content/00c8077ab0d145ca2acdef3db1bfbcef33f1cd3da0d2e9ccc2dab7d4f8ce23e5-attempt-1.txt"
    transcription_run = experiment / "pages/T0103p4/transcribe-lines/43853111fc3143e19c3afe96ac60689d"
    paths = [failure, transcription_run / "execution.json", transcription_run / "transcription.json",
             evidence / "line-transcription-input/manifest.json"]
    if not all(path.is_file() for path in paths):
        pytest.skip("isolated live evidence is not present in this checkout")
    original = failure.read_bytes()
    raw = parse_model_json(original.decode())
    unchanged = deepcopy(raw)
    run = json.loads(paths[1].read_text())
    plan_path = (experiment / run["locator_execution"]).with_name("region-plan.json")
    image_size = json.loads(plan_path.read_text())["image_size"]
    transcription = json.loads(paths[2].read_text())
    bands = json.loads(paths[3].read_text())["bands"]
    args = (raw, transcription, bands, run["notes_region"], image_size)
    assert diagnose_relations(*args) == [
        {"code": "INVALID_COMPARATOR", "location": "measurements[0].comparator.quote"},
        {"code": "EXPECTED_TEXT", "location": "measurements[1].comparator.quote"},
        {"code": "QUOTE_NOT_FOUND", "location": "measurements[1].property"},
    ]
    with pytest.raises(LayoutContextError, match="INVALID_COMPARATOR"):
        validate_relations(*args)
    assert raw == unchanged and failure.read_bytes() == original
