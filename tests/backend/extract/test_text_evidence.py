"""Independent offline evidence checks; synthetic cases are not quality gold."""
from copy import deepcopy
import json

import pytest

from backend.extract.text_evidence import apple_vision_records, infer_label_links, match_text, match_title


def box(x0=.2, y0=.4, x1=.3, y1=.42):
    return dict(x0=x0, y0=y0, x1=x1, y1=y1)


def record(text, *, bbox=None, confidence=.9, identity="r", **extra):
    return {"record_id": identity, "text": text, "bbox": bbox or box(), "confidence": confidence,
            "source": {"kind": "offline_test_ocr", "source_image_width": 1000,
                       "source_image_height": 1000, "source_image_sha256": "test-image-sha"}, **extra}


def title_rows(value="DOC-A7", confidence=.9):
    return [record("图号：", bbox=box(.1, .4, .15, .42), confidence=confidence, identity="label"),
            record(value, bbox=box(.16, .4, .28, .42), confidence=confidence, identity="value")]


def test_exact_whitespace_only_and_input_retained_without_mutation():
    rows = [record("间距 1 2 0 mm")]
    before = deepcopy(rows)
    result = match_text("120 mm", rows)
    assert result["status"] == "anchored"
    assert result["candidates"][0]["matched_original_text"] == "1 2 0 mm"
    assert result["ocr_records"] == before and rows == before
    assert result["identity_confirmed"] is False
    result["ocr_records"][0]["text"] = "mutated copy"
    assert rows == before


@pytest.mark.parametrize("model,ocr", [("A-S7", "A-57"), ("A-007", "A-7"), ("doc-A", "DOC-A"),
                                     ("80", "180"), ("80", "800"), ("80mm", "180mm")])
def test_no_character_correction_case_folding_zero_filling_or_numeric_substring(model, ocr):
    assert match_text(model, [record(ocr)])["status"] == "unanchored"


def test_missing_text_does_not_guess_from_filename_source_or_model_box():
    result = match_text("DOC-Z9", [record("other", source={"filename": "DOC-Z9.png"})], model_bbox=box())
    assert result["status"] == "unanchored" and not result["model_bbox_used_for_selection"]


@pytest.mark.parametrize("model,ocr", [("250", "-250"), ("250", "±250"), ("250", "≥250"),
                                     ("6", "6.0"), ("6", "0.6"), ("6", "16"), ("6", "6e3")])
def test_bare_number_does_not_claim_signed_qualified_decimal_or_larger_numeric_value(model, ocr):
    result = match_text(model, [record(ocr)])
    assert result["status"] != "anchored"
    assert result["semantic_equivalence_verified"] is False


@pytest.mark.parametrize("value", ["-250", "±250", "≥250", "6.0", "0.6"])
def test_complete_numeric_text_with_its_qualifier_can_match_as_text(value):
    result = match_text(value, [record(value)])
    assert result["status"] == "anchored" and result["semantic_equivalence_verified"] is False


def test_number_next_to_unit_is_only_text_evidence_not_semantic_unit_or_object_validation():
    result = match_text("80", [record("anchor 80mm")])
    assert result["status"] == "anchored"
    assert result["semantic_equivalence_verified"] is False and result["glyph_accuracy_verified"] is False
    assert result["candidates"][0]["text"] == "anchor 80mm"


def test_multiple_positions_same_number_stay_ambiguous_despite_model_box():
    rows = [record("120", bbox=box(.1, .1, .15, .12)), record("120", bbox=box(.7, .7, .75, .72))]
    result = match_text("120", rows, model_bbox=rows[1]["bbox"])
    assert result["status"] == "ambiguous" and len(result["candidates"]) == 2


def test_two_occurrences_within_one_line_are_not_one_character_position():
    result = match_text("120", [record("120 x 120")])
    assert result["status"] == "ambiguous"
    assert result["candidates"][0]["bbox"] == result["candidates"][1]["bbox"]
    assert result["candidates"][0]["text_span_codepoints"] != result["candidates"][1]["text_span_codepoints"]


def test_long_line_only_reports_observation_box_not_fictional_character_box():
    result = match_text("240", [record("length 240; count 6; width 30")])
    candidate = result["candidates"][0]
    assert candidate["bbox_precision"] == "ocr_observation_region; not a character box"
    assert candidate["range_evidence"]["coverage"] == "unavailable"
    assert candidate["range_evidence"]["interpolated_or_synthetic_boxes"] is False


def test_utf16_grapheme_ranges_retained_verbatim_and_caveated():
    ranges = [{"text": "😀", "start_utf16": 0, "length_utf16": 2, "status": "returned_by_api", "bbox": box(.1, .1, .12, .12)},
              {"text": "7", "start_utf16": 2, "length_utf16": 1, "status": "returned_by_api", "bbox": box(.12, .1, .14, .12)}]
    result = match_text("😀7", [record("😀7", range_boxes=ranges)])
    candidate = result["candidates"][0]
    assert candidate["text_span_codepoints"] == [0, 2] and candidate["text_span_utf16"] == [0, 3]
    assert candidate["range_evidence"]["coverage"] == "complete"
    assert candidate["range_evidence"]["ranges"] == ranges
    assert "not guaranteed tight glyph" in candidate["range_evidence"]["precision_note"]


def test_nil_wrong_text_or_invalid_range_never_fabricates_a_box():
    ranges = [{"text": "7", "start_utf16": 0, "length_utf16": 1, "status": "nil_from_api"},
              {"text": "wrong", "start_utf16": 0, "length_utf16": 1, "status": "returned_by_api", "bbox": box()}]
    candidate = match_text("7", [record("7", range_boxes=ranges)])["candidates"][0]
    assert candidate["range_evidence"]["coverage"] == "unavailable"
    assert len(candidate["range_evidence"]["ranges"]) == 1


def test_combining_grapheme_and_surrogate_split_range_remain_safe():
    text = "😀e\u0301"
    ranges = [{"text": "😀", "start_utf16": 0, "length_utf16": 1,
               "status": "returned_by_api", "bbox": box()},  # invalid split surrogate
              {"text": "e\u0301", "start_utf16": 2, "length_utf16": 2,
               "status": "returned_by_api", "bbox": box()}]
    result = match_text("e\u0301", [record(text, range_boxes=ranges)])
    candidate = result["candidates"][0]
    assert candidate["text_span_codepoints"] == [1, 3] and candidate["text_span_utf16"] == [2, 4]
    assert candidate["range_evidence"]["coverage"] == "complete"
    assert match_text("é", [record(text)])["status"] == "unanchored"  # no Unicode composition repair
    assert match_text("e", [record(text)])["status"] == "unanchored"  # cannot silently strip the accent
    assert match_text("\u0301", [record(text)])["status"] == "unanchored"
    emoji = match_text("😀", [record(text, range_boxes=ranges)])["candidates"][0]
    assert emoji["range_evidence"]["coverage"] == "unavailable"


@pytest.mark.parametrize("bbox", [{"x0": 0, "y0": 0, "x1": float("nan"), "y1": 1},
                                   {"x0": 0, "y0": 0, "x1": 2, "y1": 1}])
def test_invalid_position_is_not_anchored_or_clamped(bbox):
    result = match_text("READ", [record("READ", bbox=bbox)])
    assert result["status"] == "unanchored"
    assert result["candidates"][0]["positioned"] is False


def test_title_requires_explicit_label_not_confidence_or_credential_number():
    assert match_title("DOC-A7", [record("DOC-A7", confidence=1)])["status"] == "unanchored"
    rows = title_rows()
    rows[0]["text"] = "资质号"
    assert match_title("DOC-A7", rows)["status"] == "unanchored"


def test_title_unique_labeled_exact_value_is_candidate_not_human_confirmation():
    result = match_title("D O C-A7", title_rows())
    assert result["status"] == "anchored" and result["title_anchor_usable"] is True
    assert result["identity_confirmed"] is False and result["manual_confirmation_required"] is True
    link = result["layout"]["links"][0]
    assert link["kind"] == "layout_candidate" and all(link["conditions"].values())
    assert link["label"]["source"] and link["value"]["bbox"]


def test_title_conflict_keeps_model_and_ocr_independent_without_correction():
    result = match_title("DOC-AS", title_rows("DOC-A5"))
    assert result["status"] == "conflict"
    assert result["model_text"] == "DOC-AS" and result["ocr_text"] == "DOC-A5"
    assert not result["title_anchor_usable"]


@pytest.mark.parametrize("confidence", [.3, .5499, None])
def test_low_or_missing_confidence_never_grants_usable_title(confidence):
    result = match_title("DOC-A7", title_rows(confidence=confidence))
    assert result["status"] == "low_confidence_pending" and not result["title_anchor_usable"]


def test_low_label_confidence_cannot_be_replaced_by_high_value_confidence():
    rows = title_rows()
    rows[0]["confidence"] = .3
    assert match_title("DOC-A7", rows)["status"] == "low_confidence_pending"


def test_multiple_layout_values_ambiguous_even_if_model_equals_only_one():
    rows = title_rows()
    rows.append(record("OTHER", bbox=box(.16, .425, .28, .445), identity="second-value"))
    # Both baselines satisfy the bound because the second value has a larger
    # explicit observation height, yet neither overlaps another text record.
    rows[2]["bbox"] = box(.29, .395, .32, .425)
    rows[0]["bbox"] = box(.1, .4, .15, .44)
    result = match_title("DOC-A7", rows)
    assert result["status"] == "ambiguous" and len(result["candidates"]) == 2


@pytest.mark.parametrize("newbox,condition", [(box(.01, .4, .09, .42), "right_of_label"),
                                              (box(.16, .5, .28, .52), "same_baseline"),
                                              (box(.5, .4, .6, .42), "within_horizontal_gap")])
def test_layout_bounds_are_audited_and_not_relaxed(newbox, condition):
    rows = title_rows()
    rows[1]["bbox"] = newbox
    result = infer_label_links(rows)
    assert result["status"] == "unanchored"
    assert result["labels"][0]["evaluations"][0]["conditions"][condition] is False


def test_other_text_overlap_rejects_value_and_crossed_label_rejects_far_field():
    rows = title_rows()
    rows.append(record("extra", bbox=box(.2, .405, .24, .415)))
    result = infer_label_links(rows)
    first = result["labels"][0]["evaluations"][0]
    assert first["conditions"]["no_overlap_with_other_text"] is False
    rows = title_rows()
    rows.insert(1, record("比例", bbox=box(.152, .4, .158, .42)))
    result = infer_label_links(rows)
    assert result["status"] == "unanchored"
    assert result["labels"][0]["evaluations"][1]["conditions"]["no_crossing_another_label"] is False


def test_missing_dimensions_layout_unavailable_until_explicit_common_image_size():
    rows = title_rows()
    for row in rows:
        del row["source"]["source_image_width"]
        del row["source"]["source_image_height"]
    result = match_title("DOC-A7", rows)
    assert result["status"] == "unanchored"
    assert result["layout"]["image_context"]["metric"] == "unavailable"
    assert match_title("DOC-A7", rows, image_size=(1000, 1000))["status"] == "anchored"


@pytest.mark.parametrize("field,value", [("source_image_width", 1100), ("source_image_sha256", "another-image")])
def test_inconsistent_sizes_or_other_image_sha_prevent_cross_image_links(field, value):
    rows = title_rows()
    rows[1]["source"][field] = value
    result = infer_label_links(rows)
    assert result["status"] == "unanchored" and not result["links"]
    assert result["image_context"]["available"] is False


def test_non_square_image_compares_gap_and_line_height_in_actual_pixels():
    rows = title_rows()
    rows[1]["bbox"] = box(.18, .4, .28, .42)  # gap .03 x image width
    for row in rows:
        row["source"]["source_image_width"] = 4000
        row["source"]["source_image_height"] = 1000
    result = infer_label_links(rows)
    evaluation = result["labels"][0]["evaluations"][0]
    assert evaluation["geometry"]["horizontal_gap"] == pytest.approx(120)
    assert evaluation["geometry"]["max_line_height"] == pytest.approx(20)
    assert not evaluation["conditions"]["within_horizontal_gap"] and not result["links"]


@pytest.mark.parametrize("text", ["图名", "比例", "资质号", "资质号：CERT-A7", "注册执业号：CERT-A7", "资质号 CERT-A7"])
def test_another_field_is_not_treated_as_drawing_value(text):
    rows = title_rows(text)
    assert match_title(text, rows)["status"] == "unanchored"


def test_probe_adapter_rank_zero_retains_alternatives_originals_and_utf16_api_boxes():
    observation = {"index": 6, "bbox_top_left_normalized": box(), "candidates": [
        {"rank": 0, "text": "A", "confidence": .3,
         "character_ranges": [{"text": "A", "utf16_location": 0, "utf16_length": 1,
                                "status": "returned_by_api", "bbox_top_left_normalized": box()}]},
        {"rank": 1, "text": "B", "confidence": .2}]}
    probe = {"probe_version": "local-test", "source_sha256_before": "source-sha", "observations": [observation]}
    before = deepcopy(probe)
    records = apple_vision_records(probe)
    assert records[0]["text"] == "A" and records[0]["confidence"] == .3
    assert records[0]["alternatives"] == [observation["candidates"][1]]
    assert records[0]["raw_observation"] == observation
    assert records[0]["source"]["kind"] == "apple_vision_ocr"
    assert records[0]["range_boxes"][0]["start_utf16"] == 0
    assert probe == before
    assert match_text("B", records)["status"] == "unanchored"
    json.dumps(match_text("A", records), allow_nan=False)


@pytest.mark.parametrize("title", ["图纸目录", "图纸文件目录", " 图 纸 目 录 ： "])
def test_explicit_catalog_scope_prevents_title_anchor_even_for_unique_high_confidence_value(title):
    rows = title_rows("SHEET-X")
    rows.append(record(title, bbox=box(.4, .1, .6, .13), identity="catalog-heading"))
    layout = infer_label_links(rows)
    assert layout["status"] == "catalog_scope_unverified" and not layout["links"]
    assert layout["layout_candidates"][0]["geometry_eligible"] is True
    assert layout["layout_candidates"][0]["eligible"] is False
    result = match_title("SHEET-X", rows)
    assert result["status"] == "unanchored" and result["reason"] == "catalog_scope_unverified"
    assert not result["title_anchor_usable"] and result["ocr_records"] == rows


def test_catalog_plus_separate_title_block_without_verified_region_is_still_blocked():
    rows = title_rows("SHEET-X")
    rows.append(record("图纸文件目录", bbox=box(.1, .05, .4, .08)))
    result = match_title("SHEET-X", rows)
    assert result["status"] == "unanchored"
    assert result["layout"]["catalog_scope"]["independent_title_block_region_verified"] is False
    assert result["layout"]["catalog_scope"]["region_exception_supported"] is False


@pytest.mark.parametrize("field", ["文件名称", "序号", "张数", "备注", "文件编号", "图纸张数"])
def test_catalog_column_labels_cannot_be_the_drawing_value_even_without_catalog_heading(field):
    rows = title_rows(field, confidence=1)
    result = match_title(field, rows)
    assert result["status"] == "unanchored" and not result["title_anchor_usable"]
    evaluation = result["layout"]["labels"][0]["evaluations"][0]
    assert evaluation["conditions"]["value_is_not_another_field_label"] is False


def test_catalog_heading_and_drawing_file_name_header_are_plain_structure_regression():
    rows = title_rows("文件名称", confidence=1)
    rows.append(record("图纸目录", bbox=box(.1, .05, .4, .08)))
    result = match_title("文件名称", rows)
    assert result["status"] == "unanchored" and result["reason"] == "catalog_scope_unverified"
    assert result["layout"]["labels"][0]["evaluations"][0]["conditions"]["value_is_not_another_field_label"] is False


def test_unknown_page_is_not_classified_from_filename_and_normal_title_is_unchanged():
    rows = title_rows()
    for row in rows:
        row["source"]["filename"] = "图纸目录.pdf"
    result = match_title("DOC-A7", rows)
    assert result["status"] == "anchored"
    assert result["layout"]["catalog_scope"]["status"] == "no_explicit_catalog_title_observed"
    assert result["layout"]["catalog_scope"]["filename_used_for_classification"] is False


def test_mentioning_catalog_in_a_sentence_is_not_an_explicit_catalog_title():
    rows = title_rows()
    rows.append(record("请依据图纸目录核对资料", bbox=box(.4, .1, .7, .13)))
    assert match_title("DOC-A7", rows)["status"] == "anchored"
