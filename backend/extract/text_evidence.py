"""Offline OCR evidence checks; deliberately not wired into production extraction.

Matching removes whitespace only. OCR and model readings remain independent;
neither geometric candidates nor exact matches constitute human confirmation.
All coordinates are normalized with a top-left origin. Observation rectangles
are region-level evidence. API range boxes are retained as approximate UI boxes,
never promoted to tight glyph outlines or engineering measurement evidence.
"""
from __future__ import annotations

from copy import deepcopy
import math
import re
import unicodedata

VERSION = "text-evidence-offline-v2"
TITLE_LABELS = frozenset({"图号", "图纸编号"})
CATALOG_TITLES = frozenset({"图纸目录", "图纸文件目录"})
OTHER_LABELS = frozenset({"图名", "图纸名称", "比例", "资质", "资质号", "资质编号", "资格证号",
                          "设计证书", "设计单位", "专业", "审核", "校对", "设计", "日期", "图幅", "阶段", "版本",
                          "文件名称", "序号", "张数", "备注", "文件编号", "图纸张数", "图纸名称及内容"})
RANGE_CAVEAT = "API-returned approximate UI range boxes; not guaranteed tight glyph fits or engineering measurement evidence"


def _compact(text):
    return "".join(character for character in text if not character.isspace())


def _label(text):
    return _compact(text).replace(":", "").replace("：", "")


def _box(value):
    return (isinstance(value, dict) and all(type(value.get(key)) in {int, float}
            and math.isfinite(value[key]) and 0 <= value[key] <= 1 for key in ("x0", "y0", "x1", "y1"))
            and value["x0"] < value["x1"] and value["y0"] < value["y1"])


def _confidence(record):
    value = record.get("confidence")
    return value if type(value) in {int, float} and math.isfinite(value) and 0 <= value <= 1 else None


def _ref(record, index):
    return {"record_id": record.get("record_id", str(index)), "record_index": index,
            "text": record.get("text"), "bbox": deepcopy(record.get("bbox")),
            "source": deepcopy(record.get("source")), "confidence": record.get("confidence"),
            "bbox_precision": "ocr_observation_region; not a character box"}


def _report(model_text, records):
    if not isinstance(model_text, str) or not isinstance(records, (list, tuple)):
        raise ValueError("model_text must be a string and records a sequence")
    if any(not isinstance(record, dict) or not isinstance(record.get("text"), str) for record in records):
        raise ValueError("every OCR record must contain string text")
    return {"version": VERSION, "model_text": model_text, "ocr_records": deepcopy(list(records)),
            "normalization": "whitespace only; no character replacement, case folding, numeric repair or filename inference",
            "identity_confirmed": False, "manual_confirmation_required": True,
            "quality_status": "unassessed", "semantic_equivalence_verified": False,
            "glyph_accuracy_verified": False, "candidates": []}


def _utf16_offset(text):
    return len(text.encode("utf-16-le")) // 2


def _range_evidence(record, positions):
    """Return actual supplied range boxes without interpolating or unioning boxes."""
    text = record["text"]
    required = {_utf16_offset(text[:position]) for position in positions}
    covered, selected = set(), []
    for item in record.get("range_boxes", []):
        start, length = item.get("start_utf16"), item.get("length_utf16")
        if type(start) is not int or type(length) is not int or start < 0 or length <= 0:
            continue
        relevant = {position for position in required if start <= position < start + length}
        if not relevant:
            continue
        try:
            original = text.encode("utf-16-le")[start * 2:(start + length) * 2].decode("utf-16-le")
        except UnicodeDecodeError:
            continue
        # A caller cannot attach another word's returned rectangle to this range.
        if original != item.get("text"):
            continue
        selected.append(deepcopy(item))
        if item.get("status") == "returned_by_api" and _box(item.get("bbox")):
            covered.update(relevant)
    return {"ranges": selected, "coverage": "complete" if required and covered == required else "partial" if covered else "unavailable",
            "precision_note": RANGE_CAVEAT, "interpolated_or_synthetic_boxes": False}


def match_text(model_text, records, *, model_bbox=None):
    """Enumerate every exact text occurrence; model bbox never resolves ambiguity."""
    result = _report(model_text, records)
    target = _compact(model_text)
    numeric_target = bool(re.fullmatch(r"\d+(?:[.,]\d+)?", target))
    numeric_qualifiers = frozenset("+-−–—±∓<>=≤≥≠≈~～≦≧.,，．٫:：/⁄eE")
    result["model_bbox_used_for_selection"] = False
    if model_bbox is not None:
        result["model_bbox"] = deepcopy(model_bbox)
    if not target:
        result.update(status="unanchored", reason="empty_after_whitespace_normalization")
        return result
    for index, record in enumerate(records):
        positions = [offset for offset, character in enumerate(record["text"]) if not character.isspace()]
        compact = "".join(record["text"][offset] for offset in positions)
        start = 0
        while (found := compact.find(target, start)) >= 0:
            stop = found + len(target)
            start = found + 1
            # A number within a longer number is not the same numeric reading.
            if ((target[0].isdigit() and found > 0 and compact[found - 1].isdigit()) or
                    (target[-1].isdigit() and stop < len(compact) and compact[stop].isdigit())):
                continue
            # Do not treat a base character stripped from its combining mark,
            # or a combining mark alone, as the same complete text reading.
            if unicodedata.combining(compact[found]) or (stop < len(compact) and unicodedata.combining(compact[stop])):
                continue
            matched_positions = positions[found:stop]
            qualifier_mismatch = numeric_target and (
                (found > 0 and compact[found - 1] in numeric_qualifiers) or
                (stop < len(compact) and compact[stop] in numeric_qualifiers))
            first, last = matched_positions[0], matched_positions[-1] + 1
            candidate = _ref(record, index)
            candidate.update(text_span_codepoints=[first, last],
                             text_span_utf16=[_utf16_offset(record["text"][:first]), _utf16_offset(record["text"][:last])],
                             matched_original_text=record["text"][first:last],
                             positioned=_box(record.get("bbox")),
                             numeric_qualifier_mismatch=qualifier_mismatch,
                             semantic_equivalence_verified=False, glyph_accuracy_verified=False,
                             range_evidence=_range_evidence(record, matched_positions))
            result["candidates"].append(candidate)
    count = len(result["candidates"])
    result["status"] = "ambiguous" if count > 1 else "anchored" if count == 1 and result["candidates"][0]["positioned"] and not result["candidates"][0]["numeric_qualifier_mismatch"] else "unanchored"
    result["reason"] = "multiple_exact_occurrences" if count > 1 else "unique_exact_positioned_occurrence" if result["status"] == "anchored" else "numeric_qualifier_mismatch" if count == 1 and result["candidates"][0]["numeric_qualifier_mismatch"] else "no_unique_positioned_text_evidence"
    return result


def _intersects(left, right):
    return min(left["x1"], right["x1"]) > max(left["x0"], right["x0"]) and min(left["y1"], right["y1"]) > max(left["y0"], right["y0"])


def _is_other_field(text):
    compact = _compact(text)
    label = _label(text)
    if label in TITLE_LABELS | OTHER_LABELS:
        return True
    # Explicit other field labels and their values remain other fields, even if
    # their value happens to resemble a drawing number. No number pattern is used.
    # A whole other labeled field is not the drawing value. Conservatively
    # exclude colon-delimited fields, including labels outside this small list.
    return any(compact.startswith(name) for name in TITLE_LABELS | OTHER_LABELS) or ":" in compact or "：" in compact


def _image_context(records, image_size=None):
    """A common image frame is required before comparing horizontal/vertical units."""
    def valid_size(size):
        return isinstance(size, (list, tuple)) and len(size) == 2 and all(type(v) is int and v > 0 for v in size)
    sizes, hashes = [], []
    for record in records:
        source = record.get("source")
        source = source if isinstance(source, dict) else {}
        sizes.append([source.get("source_image_width", source.get("image_width")),
                      source.get("source_image_height", source.get("image_height"))])
        hashes.append(source.get("source_image_sha256", source.get("source_sha256")))
    if image_size is not None:
        expected = list(image_size) if valid_size(image_size) else None
        sizes_valid = expected is not None and all(not any(value is not None for value in size) or size == expected for size in sizes)
    else:
        expected = sizes[0] if sizes and valid_size(sizes[0]) else None
        sizes_valid = expected is not None and all(size == expected for size in sizes)
    known_hashes = sorted(set(value for value in hashes if isinstance(value, str) and value))
    return {"available": bool(sizes_valid and len(known_hashes) <= 1), "image_size": expected,
            "source_image_sha256_values": known_hashes, "image_sha_conflict": len(known_hashes) > 1,
            "image_size_missing_or_inconsistent": not sizes_valid,
            "metric": "source_image_pixels" if sizes_valid and len(known_hashes) <= 1 else "unavailable"}


def _metric_box(record, context):
    """Compare x/y in pixels when actual image dimensions are available."""
    bbox = record.get("bbox")
    if not _box(bbox):
        return None, "invalid", None
    if context["available"]:
        width, height = context["image_size"]
        return {key: value * (width if key[0] == "x" else height) for key, value in bbox.items()}, "source_image_pixels", [width, height]
    return None, "unavailable", None


def infer_label_links(records, *, image_size=None):
    """Generate auditable layout candidates, never confirmed label/value links.

    Bounds: value lies right of the label; center-y difference <= half the
    larger line height; horizontal gap <= four times that height. Every eligible
    value is retained. No nearest-candidate selection or identifier regex.
    """
    result = _report("", records)
    context = _image_context(records, image_size)
    catalog_titles = [_ref(record, index) for index, record in enumerate(records)
                      if _label(record["text"]) in CATALOG_TITLES]
    scope_status = "catalog_scope_unverified" if catalog_titles else "no_explicit_catalog_title_observed"
    result.update(kind="layout_candidate", labels=[], links=[], layout_candidates=[], confidence_threshold=0.55,
                  catalog_scope={"status": scope_status, "title_observations": catalog_titles,
                                 "independent_title_block_region_verified": False,
                                 "region_exception_supported": False,
                                 "filename_used_for_classification": False,
                                 "limitation": "catalog pages stay unanchored; this version does not accept an independently verified title-block-region exception"},
                  image_context=context,
                  geometry_policy={"maximum_center_y_difference_line_heights": 0.5,
                                   "maximum_horizontal_gap_line_heights": 4})
    for label_index, label_record in enumerate(records):
        if _label(label_record["text"]) not in TITLE_LABELS:
            continue
        entry = {"label": _ref(label_record, label_index), "evaluations": [], "eligible_value_indices": []}
        result["labels"].append(entry)
        original_left = label_record.get("bbox")
        left, left_metric, left_size = _metric_box(label_record, context)
        for value_index, value_record in enumerate(records):
            if value_index == label_index:
                continue
            original_value = value_record.get("bbox")
            value, value_metric, value_size = _metric_box(value_record, context)
            compatible = context["available"] and left_metric == value_metric and left_size == value_size
            conditions = {"valid_label_bbox": _box(original_left), "valid_value_bbox": _box(original_value),
                          "compatible_coordinate_metrics": compatible,
                          "value_is_not_another_field_label": not _is_other_field(value_record["text"]),
                          "nonempty_value": bool(_compact(value_record["text"]))}
            details = {}
            if _box(original_left) and _box(original_value) and compatible:
                height = max(left["y1"] - left["y0"], value["y1"] - value["y0"])
                center_diff = abs((left["y0"] + left["y1"] - value["y0"] - value["y1"]) / 2)
                gap = value["x0"] - left["x1"]
                overlaps = [i for i, other in enumerate(records) if i not in {label_index, value_index}
                            and _box(other.get("bbox")) and _intersects(original_value, other["bbox"])]
                corridor = {"x0": original_left["x1"], "x1": original_value["x0"],
                            "y0": min(original_left["y0"], original_value["y0"]), "y1": max(original_left["y1"], original_value["y1"])}
                crossing = [i for i, other in enumerate(records) if i not in {label_index, value_index}
                            and (_label(other["text"]) in TITLE_LABELS | OTHER_LABELS or _is_other_field(other["text"]))
                            and _box(other.get("bbox")) and corridor["x0"] < corridor["x1"]
                            and _intersects(corridor, other["bbox"])]
                conditions.update(right_of_label=gap >= 0, same_baseline=center_diff <= 0.5 * height,
                                  within_horizontal_gap=gap <= 4 * height,
                                  no_overlap_with_other_text=not overlaps, no_crossing_another_label=not crossing)
                details.update(max_line_height=height, center_y_difference=center_diff, horizontal_gap=gap,
                               metric=left_metric, source_image_size=left_size,
                               maximum_center_y_difference=0.5 * height, maximum_horizontal_gap=4 * height,
                               overlapping_record_indices=overlaps, crossed_label_indices=crossing)
            geometry_eligible = all(conditions.values())
            conditions["no_explicit_catalog_scope_conflict"] = not catalog_titles
            eligible = all(conditions.values())
            confidences = [_confidence(label_record), _confidence(value_record)]
            low = any(confidence is None or confidence < 0.55 for confidence in confidences)
            evaluated = {"label": _ref(label_record, label_index), "value": _ref(value_record, value_index),
                         "conditions": conditions, "geometry": details, "eligible": eligible,
                         "geometry_eligible": geometry_eligible, "scope_status": scope_status,
                         "kind": "layout_candidate", "confidence_status": "low_confidence_pending" if low else "not_below_threshold",
                         "identity_confirmed": False}
            entry["evaluations"].append(evaluated)
            if geometry_eligible:
                result["layout_candidates"].append(deepcopy(evaluated))
            if eligible:
                entry["eligible_value_indices"].append(value_index)
                result["links"].append(deepcopy(evaluated))
        entry["status"] = "catalog_scope_unverified" if catalog_titles else "ambiguous" if len(entry["eligible_value_indices"]) > 1 else "layout_candidate" if entry["eligible_value_indices"] else "unanchored"
    result["status"] = "catalog_scope_unverified" if catalog_titles else "ambiguous" if len(result["links"]) > 1 else "layout_candidate" if result["links"] else "unanchored"
    if not context["available"]:
        result["reason"] = "common_source_image_frame_unavailable"
    return result


def match_title(model_text, records, *, image_size=None):
    """Require one explicit labeled value; never replace a conflicting reading."""
    result = _report(model_text, records)
    layout = infer_label_links(records, image_size=image_size)
    result.update(layout=layout, candidates=deepcopy(layout["links"]), title_anchor_usable=False,
                  model_bbox_used_for_selection=False)
    if layout["catalog_scope"]["status"] == "catalog_scope_unverified":
        result.update(status="unanchored", reason="catalog_scope_unverified",
                      candidates=deepcopy(layout["layout_candidates"]))
    elif not layout["links"]:
        result.update(status="unanchored", reason="no_unique_explicit_label_value_layout_evidence")
    elif len(layout["links"]) != 1:
        result.update(status="ambiguous", reason="multiple_eligible_labeled_positions")
    else:
        link = layout["links"][0]
        value = link["value"]
        result["ocr_text"] = value["text"]
        result["confidence_status"] = link["confidence_status"]
        if _compact(model_text) != _compact(value["text"]):
            result.update(status="conflict", reason="model_and_explicit_labeled_ocr_value_disagree")
        elif link["confidence_status"] == "low_confidence_pending":
            result.update(status="low_confidence_pending", reason="label_or_value_confidence_missing_or_below_threshold")
        else:
            result.update(status="anchored", reason="unique_explicit_labeled_value_matches_character_for_character",
                          title_anchor_usable=True)
    return result


def apple_vision_records(probe):
    """Adapt actual probe rank-0 observations; preserve alternatives and originals."""
    records = []
    for ordinal, observation in enumerate(probe.get("observations", [])):
        candidates = observation.get("candidates", [])
        primary = next((candidate for candidate in candidates if candidate.get("rank") == 0), None)
        if primary is None or not isinstance(primary.get("text"), str):
            primary = {"text": "", "confidence": None}
        index = observation.get("index", ordinal)
        ranges = []
        for item in primary.get("character_ranges", []):
            ranges.append({"text": item.get("text"), "start_utf16": item.get("utf16_location"),
                           "length_utf16": item.get("utf16_length"), "status": item.get("status"),
                           "bbox": deepcopy(item.get("bbox_top_left_normalized")),
                           "precision_note": RANGE_CAVEAT, "raw_range": deepcopy(item)})
        records.append({"record_id": f"apple-vision:{index}", "text": primary["text"],
                        "bbox": deepcopy(observation.get("bbox_top_left_normalized")),
                        "confidence": primary.get("confidence"), "range_boxes": ranges,
                        "source": {"kind": "apple_vision_ocr", "probe_version": probe.get("probe_version"),
                                   "source_sha256": probe.get("source_sha256_before"), "observation_index": index,
                                   "platform": probe.get("platform"), "coordinate_system": "top_left_normalized",
                                   "source_image_width": probe.get("image_width"), "source_image_height": probe.get("image_height"),
                                   "probe_metadata": deepcopy({key: value for key, value in probe.items() if key != "observations"}),
                                   "primary_rank": 0, "request": deepcopy(probe.get("request"))},
                        "alternatives": deepcopy([candidate for candidate in candidates if candidate.get("rank") != 0]),
                        "raw_observation": deepcopy(observation)})
    return records
