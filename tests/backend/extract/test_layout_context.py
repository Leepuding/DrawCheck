from copy import deepcopy
import json

import pytest

from backend.extract.layout_context import (
    REGION_TYPES, LayoutContextError, build_anchored_notes_prompt, build_region_prompt, plan_regions,
    summarize, validate_region,
)
from backend.extract.line_regions import bind_note_lines


SIZE = (1000, 800)


def box(x0=.1, y0=.1, x1=.9, y1=.9):
    return dict(x0=x0, y0=y0, x1=x1, y1=y1)


def area(kind, bbox=None, context=None):
    bbox = bbox or box()
    return dict(region_type=kind, bbox=bbox, context_bbox=context or bbox, boundary_status="complete")


def line(key, text, bbox=None, paragraph=None):
    return dict(id=key, text=text, bbox=bbox or box(), paragraph_id=paragraph)


def quote(line_id, text, **kwargs):
    return dict(line_id=line_id, quote=text, **kwargs)


def measurement(key, literal, value, *, unit=None, comparator=None, prop=None, subject=None,
                origin="unknown", basis="unresolved", **kwargs):
    return dict(id=key, literal=literal, value=value, unit=unit, comparator=comparator,
                property=prop, object=subject, unit_origin=origin, relation_basis=basis, **kwargs)


def response(lines, measurements, **kwargs):
    return dict(context_status="complete", lines=lines, measurements=measurements, **kwargs)


def validate(payload, kind="notes"):
    plan = plan_regions({"regions": [area(kind)]}, SIZE)
    return validate_region(payload, plan["regions"][0], SIZE)


@pytest.mark.parametrize("kind", REGION_TYPES)
def test_region_prompt_contains_literal_json_that_passes_its_own_validator(kind):
    prompt = build_region_prompt(kind)
    example, _ = json.JSONDecoder().raw_decode(prompt[prompt.index("{"):])
    result = validate(example, kind)
    assert result["region_type"] == kind
    assert "合成示例" in prompt
    assert "Span" not in prompt
    assert "start" not in prompt


def test_anchored_notes_example_uses_host_geometry_and_real_input_line_ids():
    fixed = {"lines": [{"id": "ink-11", "bbox": box(.0, .2, 1., .3)},
                       {"id": "ink-12", "bbox": box(.0, .4, 1., .5)}]}
    prompt = build_anchored_notes_prompt(fixed)
    example, _ = json.JSONDecoder().raw_decode(prompt[prompt.index("{"):])
    assert '"bbox"' not in json.dumps(example)
    assert prompt.index('"ink-11"') < prompt.index('"ink-12"')
    assert '"paragraph_id"' in prompt
    example_geometry = {"lines": [{"id": "demo-line", "bbox": box(.0, .2, 1., .3),
                                   "touches_crop_edge": False}], "rejected_bands": []}
    result = validate(bind_note_lines(example, example_geometry))
    assert result["measurements"][0]["value"]["bbox_precision"] == "line"
    assert result["measurements"][0]["value"]["quote"] == "12"


def test_plan_keeps_context_pixels_budget_and_unlocated_scope():
    notes = area("notes", box(.2, .3, .6, .7), box(.15, .25, .8, .95))
    payload = {"regions": [area("dimension_chain"), notes, area("notes")],
               "unlocated": [{"region_type": "other", "reason": "局部看不清"}]}
    plan = plan_regions(payload, SIZE, max_regions=2)
    assert [row["region_type"] for row in plan["regions"]] == ["notes", "dimension_chain", "notes"]
    assert [row["state"] for row in plan["regions"]] == ["planned", "planned", "not_scheduled"]
    assert plan["regions"][2]["pixel_box"] == [150, 200, 800, 760]
    assert plan == plan_regions({**payload, "regions": payload["regions"][::-1]}, SIZE, max_regions=2)
    result = summarize(plan, {})
    assert result["validated_regions"] == 0 and result["coverage"] == "unknown" and not result["whole_complete"]
    assert {item["reason"] for item in result["coverage_gaps"]} == {
        "whole_page_coverage_unverified", "局部看不清", "not_scheduled", "missing_or_failed_region"}


def test_plan_rejects_context_cutting_primary_region():
    with pytest.raises(LayoutContextError, match="CONTEXT_EXCLUDES_REGION"):
        plan_regions({"regions": [area("notes", context=box(.15, .15, .85, .85))]}, SIZE)


def test_repeated_numeric_quote_resolves_only_within_its_context_literal():
    text = "化学锚栓锚入80mm，屋面板厚80mm，不可洞穿。"
    common = dict(unit=quote("l1", "mm"), origin="explicit", basis="same_phrase")
    rows = [measurement("anchor", quote("l1", "锚入80mm"), quote("l1", "80"),
                        prop=quote("l1", "锚入"), subject=quote("l1", "化学锚栓"), **common),
            measurement("board", quote("l1", "屋面板厚80mm"), quote("l1", "80"),
                        prop=quote("l1", "厚"), subject=quote("l1", "屋面板"), **common)]
    result = validate(response([line("l1", text)], rows))
    assert result["context_text"] == text
    first, second = result["measurements"]
    assert first["value"]["start"] == text.index("80")
    assert second["value"]["start"] == text.rindex("80")
    assert first["object_ref"] != second["object_ref"]
    assert first["value"]["bbox_precision"] == "line"
    assert first["value"]["bbox"] == pytest.approx(box(.18, .18, .82, .82))
    assert not first["identity_confirmed"] and not second["identity_confirmed"]
    rows[0]["literal"] = quote("l1", "80")
    with pytest.raises(LayoutContextError, match="AMBIGUOUS_QUOTE"):
        validate(response([line("l1", text)], rows))


def test_shared_paragraph_subject_and_distinct_force_properties():
    lines = [line("l1", "单个化学锚栓", paragraph="p1"),
             line("l2", "抗拔力≥6.0kN，抗剪力≥4.0kN", paragraph="p1")]
    rows = [measurement(key, quote("l2", f"{prop}≥{value}kN"), quote("l2", value),
                        unit=quote("l2", "kN"), comparator=quote("l2", "≥"), prop=quote("l2", prop),
                        subject=quote("l1", "单个化学锚栓"), origin="explicit", basis="same_phrase")
            for key, prop, value in [("tension", "抗拔力", "6.0"), ("shear", "抗剪力", "4.0")]]
    result = validate(response(lines, rows))
    first, second = result["measurements"]
    assert first["object_ref"] == second["object_ref"]
    assert [item["property"]["quote"] for item in (first, second)] == ["抗拔力", "抗剪力"]
    assert [item["value"]["quote"] for item in (first, second)] == ["6.0", "4.0"]
    lines[0]["paragraph_id"] = "different"
    with pytest.raises(LayoutContextError, match="UNSUPPORTED_PHRASE_RELATION"):
        validate(response(lines, rows))


@pytest.mark.parametrize(("text", "value", "reason"), [
    ("2#10化学锚栓", "2", "NUMERIC_TOKEN_TRUNCATED"),
    ("抗拔力6.OKN", "6", "NUMERIC_TOKEN_TRUNCATED"),
    ("板厚≥100", "100", "OMITTED_VISIBLE_COMPARATOR"),
])
def test_bad_or_qualified_numeric_text_cannot_become_a_partial_scalar(text, value, reason):
    with pytest.raises(LayoutContextError, match=reason):
        validate(response([line("l", text)], [measurement("m", quote("l", text), quote("l", value))]))


def test_missing_inherited_unit_declaration_is_rejected():
    payload = response([line("a", "板厚100"), line("b", "单位：mm")],
                       [measurement("m", quote("a", "板厚100"), quote("a", "100"),
                                    unit=quote("b", "mm"), origin="inherited")])
    with pytest.raises(LayoutContextError, match="UNIT_DECLARATION_REQUIRED"):
        validate(payload)


def test_neighboring_measurement_cannot_be_an_inherited_unit_declaration():
    unit = measurement("m", quote("a", "板厚100"), quote("a", "100"),
                       unit=quote("b", "mm"), unit_relation=quote("b", "锚入80mm"), origin="inherited")
    with pytest.raises(LayoutContextError, match="UNIT_DECLARATION_REQUIRED"):
        validate(response([line("a", "板厚100"), line("b", "锚入80mm")], [unit]))
    unit["unit_relation"] = quote("b", "尺寸单位：mm")
    result = validate(response([line("a", "板厚100"), line("b", "尺寸单位：mm")], [unit]))
    assert result["measurements"][0]["unit_relation"]["quote"] == "尺寸单位：mm"


def test_table_duplicate_physical_rows_and_header_unit_are_preserved():
    header = [line("hc", "规格", box(.11, .11, .39, .17)),
              line("hq", "数量（根）", box(.51, .11, .79, .17))]
    columns = [{"id": "code", "bbox": box(.1, .1, .4, .8), "header": quote("hc", "规格")},
               {"id": "qty", "bbox": box(.5, .1, .8, .8), "header": quote("hq", "数量（根）")}]
    lines, rows, readings = list(header), [], []
    for index, y in enumerate((.2, .4)):
        code, qty = f"code{index}", f"qty{index}"
        lines.extend([line(code, "电缆09", box(.11, y+.01, .39, y+.06)),
                      line(qty, "1", box(.51, y+.01, .79, y+.06))])
        rows.append({"id": f"r{index}", "row_index": index, "bbox": box(.1, y, .8, y+.08),
                     "cells": [{"column_id": "code", "state": "read", "content": quote(code, "电缆09")},
                               {"column_id": "qty", "state": "read", "content": quote(qty, "1")}]})
        readings.append(measurement(f"m{index}", quote(qty, "1"), quote(qty, "1"),
                                    unit=quote("hq", "根"), unit_relation=quote("hq", "数量（根）"),
                                    prop=quote("hq", "数量"), subject=quote(code, "电缆09"),
                                    origin="inherited", basis="table_row", row_id=f"r{index}", column_id="qty"))
    payload = response(lines, readings, table={"columns": columns, "rows": rows})
    result = validate(payload, "table")
    assert len(result["table"]["rows"]) == 2
    assert result["measurements"][0]["object_ref"] != result["measurements"][1]["object_ref"]
    assert result["measurements"][0]["unit"]["quote"] == "根"
    wrong = deepcopy(payload)
    wrong["measurements"][0]["object"] = quote("code1", "电缆09")
    with pytest.raises(LayoutContextError, match="OBJECT_OUTSIDE_PHYSICAL_ROW"):
        validate(wrong, "table")
    rows[0]["cells"][1]["content"] = quote("qty1", "1")
    with pytest.raises(LayoutContextError, match="CELL_OUTSIDE_ROW_OR_COLUMN"):
        validate(payload, "table")


def test_vertical_chain_keeps_ordered_duplicate_dimensions_without_unit_or_identity_inference():
    lines, segments, readings = [], [], []
    for index, (value, y) in enumerate(zip(("250", "300", "250"), (.2, .4, .6))):
        line_id, segment_id = f"l{index}", f"s{index}"
        bounds = box(.1, y, .2, y+.07)
        lines.append(line(line_id, value, bounds))
        segments.append({"id": segment_id, "bbox": bounds, "label": quote(line_id, value)})
        readings.append(measurement(f"m{index}", quote(line_id, value), quote(line_id, value),
                                    basis="chain_segment", segment_id=segment_id))
    chain = {"direction": "vertical", "endpoints": [{"x": .2, "y": .15}, {"x": .2, "y": .75}],
             "association_evidence": [], "segments": segments}
    payload = response(lines, readings, chain=chain)
    result = validate(payload, "dimension_chain")
    assert [row["label"]["quote"] for row in result["chain"]["segments"]] == ["250", "300", "250"]
    assert all(row["unit"] is None and row["object_ref"] is None for row in result["measurements"])
    chain["segments"] = segments[::-1]
    with pytest.raises(LayoutContextError, match="CHAIN_SEGMENT_ORDER"):
        validate(payload, "dimension_chain")


def test_overlapping_different_precise_readings_remain_separate_conflict_candidates():
    plan = plan_regions({"regions": [area("notes"), area("notes")]}, SIZE)
    results = {}
    for region, value in zip(plan["regions"], ("100", "110")):
        row = measurement("m", quote("l", f"厚{value}mm", bbox=box(.3, .2, .6, .4)), quote("l", value),
                          unit=quote("l", "mm"), prop=quote("l", "厚"), subject=quote("l", "板"),
                          origin="explicit", basis="same_phrase")
        result = validate_region(response([line("l", f"板厚{value}mm")], [row]), region, SIZE)
        results[region["id"]] = result
    report = summarize(plan, results)
    assert [item["value"]["quote"] for item in report["measurements"]] == ["100", "110"]
    assert report["conflicts"][0]["differing_fields"] == ["literal", "value"]
    assert report["validated_regions"] == 2 and report["quality_status"] == "unassessed"
    assert report["coverage"] == "unknown" and not report["whole_complete"]
