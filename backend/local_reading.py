"""Independent, positioned local evidence for an old unlocatable machine reading.

This never edits a historical question or creates a human confirmation. A new
reading must identify the complete line uniquely on the same immutable image.
"""
from __future__ import annotations

import math
from collections import defaultdict

from backend.store import Source, StoreError
from backend.store.originals import sha256_file
from backend.store.paths import safe_path
from backend.title_identity import has_manual_number


_BOX_FIELDS = ("x0", "y0", "x1", "y1")
_READING_REASONS = {"看不清", "把握度低于阈值"}
_PROSE_CATEGORIES = {"说明", "施工说明", "施工要求", "设计要求", "材料规格"}


def corroborated_prose(fact, *, bbox):
    """Full literal proof for a reading-only note, with no comparison claim.

    Return the proof image hash; the caller must match the current image.
    This never clears a model flag, resolves a reference, or supplies an
    engineering decision.
    """
    from backend.extract.positioned_notes import POSITION_VERSION, complete_note_match, corroborated_note_omission
    from backend.references import visible_reference_hint

    if (fact.get("category") not in _PROSE_CATEGORIES or fact.get("numeric_value") is not None
            or any(fact.get(key) for key in (
                "object_id", "comparison_key", "identity_confirmed", "unit", "quantity_role",
                "target_drawing_number", "target_detail_number", "detail_number",
                "numeric_value_invalid", "unit_invalid", "region_conflict", "bbox_clamped",
                "region_bbox_unlocated"))):
        return None
    flags = fact.get("model_original_flags") or {}
    proof = fact.get("position_recovery") or {}
    text = fact.get("text")
    if (not isinstance(text, str) or any(ch in text for ch in "?？□�")
            or not isinstance(flags, dict) or flags.get("region_conflict")
            or not isinstance(proof, dict) or proof.get("version") != POSITION_VERSION
            or proof.get("scope") != "note" or visible_reference_hint(fact)
            or not _box(bbox)):
        return None
    if proof.get("reading_status") == "locally_transcribed":
        if not corroborated_note_omission(proof, text):
            return None
    elif proof.get("reading_status") != "unchanged" or proof.get("original_text") != text:
        return None
    locations = proof.get("locations")
    if not isinstance(locations, list) or len(locations) != 1 or not isinstance(locations[0], dict):
        return None
    location = locations[0]
    expected = {"unique_exact": "exact", "unique_format_equivalent": "note_layout_typography"}
    match_type = expected.get(proof.get("status"))
    if not match_type or location.get("match_type") != match_type:
        return None
    lines = location.get("source_lines")
    score = location.get("confidence")
    if (location.get("bbox") != bbox or location.get("literal_occurrence_count") != 1
            or type(score) not in (int, float) or not .95 <= score <= 1
            or not isinstance(lines, list) or not lines
            or not all(isinstance(line, dict) and _box(line.get("bbox"))
                       and isinstance(line.get("text"), str) and line.get("observation_id") for line in lines)):
        return None
    observed = "".join(line["text"] for line in lines)
    if observed != location.get("observed_text") or not complete_note_match(text, observed, match_type):
        return None
    return proof.get("image_sha256")


def corroborated_prose_questions(repo, version_id, *, rows, units):
    """Retire only open generic questions tied to the same machine fact and image."""
    def anchor(row):
        return (row["unit_id"], row["text"], *(row.get(key) for key in _BOX_FIELDS))

    eligible = defaultdict(list)
    for row in rows:
        if (row.get("status") == "待确认" and row.get("source") == Source.VISION.value
                and row.get("reason") in _READING_REASONS and _box(row)):
            eligible[anchor(row)].append(row["id"])
    if not eligible:
        return set()
    proofs = defaultdict(set)
    for fact in repo.list_records("facts", version_id=version_id):
        if fact.get("source") != Source.VISION.value or (key := anchor(fact)) not in eligible:
            continue
        unit = units.get(fact["unit_id"]) or {}
        meta = unit.get("metadata") or {}
        if meta.get("extract_status") != "ok" or meta.get("out_of_scope"):
            continue
        annotation = (meta.get("fact_annotations") or {}).get(fact["id"]) or {}
        proofs[key].add(corroborated_prose(
            {**annotation, **fact}, bbox={name: fact[name] for name in _BOX_FIELDS}))
    image_hashes = {}
    retired = set()
    for key, hashes in proofs.items():
        uid = key[0]
        meta = units[uid]["metadata"]
        stored_hash = meta.get("extract_image_sha256")
        if not stored_hash or hashes != {stored_hash}:
            continue
        if uid not in image_hashes:
            relative = meta.get("review_relative_path") or meta.get("preview_relative_path")
            try:
                image_hashes[uid] = sha256_file(safe_path(repo.root, relative)) if relative else None
            except (StoreError, OSError, TypeError, ValueError):
                image_hashes[uid] = None
        if image_hashes[uid] == stored_hash:
            retired.update(eligible[key])
    return retired


def _box(value):
    if not isinstance(value, dict):
        return False
    try:
        edges = [value[key] for key in ("x0", "y0", "x1", "y1")]
        if any(isinstance(edge, bool) for edge in edges):
            return False
        x0, y0, x1, y1 = map(float, edges)
        return all(math.isfinite(edge) for edge in (x0, y0, x1, y1)) and (
            0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _confident(evidence):
    try:
        score = evidence.get("confidence")
        return not isinstance(score, bool) and math.isfinite(float(score)) and .98 <= float(score) <= 1
    except (TypeError, ValueError, OverflowError):
        return False


def _current_readings(root, unit):
    meta = unit.get("metadata") or {}
    reading = meta.get("local_sheet") or {}
    if not isinstance(reading, dict) or not isinstance(reading.get("version"), str):
        return []
    if not reading["version"].startswith("rapidocr-") or meta.get("out_of_scope"):
        return []
    title = reading.get("title") or {}
    label = title.get("label") or {}
    value = title.get("value") or {}
    if not (_box(label.get("bbox")) and _box(value.get("bbox"))
            and _confident(value) and title.get("number")):
        return []
    if "".join(str(label.get("text") or "").split()) not in {"图号", "图纸编号", "图纸号", "图纸代号"}:
        return []
    try:
        path = safe_path(root, meta.get("review_relative_path") or meta.get("preview_relative_path") or "")
        if not reading.get("image_sha") or sha256_file(path) != reading["image_sha"]:
            return []
        native_pdf_valid = False
        if reading.get("pdf_sha") and meta.get("page_relative_path"):
            native_pdf_valid = sha256_file(safe_path(root, meta["page_relative_path"])) == reading["pdf_sha"]
    except (OSError, TypeError, ValueError, StoreError):
        return []
    candidates = []
    for evidence in reading.get("title_readings") or []:
        if (not isinstance(evidence, dict) or evidence.get("scope") != "title_region"
                or type(evidence.get("candidate_count")) is not int or evidence["candidate_count"] != 1
                or not _box(evidence.get("bbox")) or not _confident(evidence)):
            continue
        source, reader = evidence.get("source"), evidence.get("reader")
        if reader == "local_ocr":
            if source != Source.VISION.value:
                continue
        elif reader == "positioned_native_text":
            if source not in (Source.PDF_TEXT.value, Source.CAD_TEXT.value):
                continue
            if source == Source.PDF_TEXT.value and not native_pdf_valid:
                continue
        else:
            continue
        if evidence.get("field") == "drawing_number" and has_manual_number(unit):
            continue
        candidates.append(evidence)
    return candidates


def _original_field(row, unit, facts):
    annotations = (unit.get("metadata") or {}).get("fact_annotations") or {}
    fields = set()
    for fact in facts:
        if (fact.get("unit_id") != unit["id"] or fact.get("text") != row.get("text")
                or not all(fact.get(key) == row.get(key) for key in ("x0", "y0", "x1", "y1"))):
            continue
        field = (annotations.get(fact["id"]) or {}).get("comparison_key")
        if field in {"design_institution", "project_name", "drawing_type", "drawing_number", "drawing_name"}:
            fields.add(field)
    if len(fields) == 1:
        return fields.pop()
    meta = unit.get("metadata") or {}
    if not fields and row.get("text") == meta.get("title_drawing_number"):
        return "drawing_number"
    return None


def local_reading_recoveries(repo, version_id, *, rows=None, units=None):
    """Return auditable replacements for open machine questions with no valid box."""
    if rows is None:
        rows = repo.list_records("pending_confirmations", version_id=version_id)
    eligible = [row for row in rows if row.get("status") == "待确认"
                and row.get("source") != Source.USER.value and not _box(row)]
    if not eligible:
        return {}
    if units is None:
        units = {unit["id"]: unit for unit in repo.list_records("drawing_units", version_id=version_id)}
    by_unit = {}
    recovered = {}
    facts = None
    for row in eligible:
        unit = units.get(row.get("unit_id"))
        if unit is None:
            continue
        if unit["id"] not in by_unit:
            by_unit[unit["id"]] = _current_readings(repo.root, unit)
        literal = str(row.get("text") or "").strip()
        matches = [evidence for evidence in by_unit[unit["id"]]
                   if str(evidence.get("text") or "").strip() == literal]
        if not literal or len(matches) != 1:
            continue
        evidence = matches[0]
        if facts is None:
            facts = repo.list_records("facts", version_id=version_id)
        field = _original_field(row, unit, facts)
        if not field:
            continue
        # Collapsed boxes can be shared by unrelated title fields. A processed
        # decision only outranks this reading when its original field is bound
        # to the same unit and anchor, even if the old machine literal differs.
        if any(other.get("status") != "待确认" and other.get("unit_id") == row.get("unit_id")
               and all(other.get(key) == row.get(key) for key in ("x0", "y0", "x1", "y1"))
               and _original_field(other, unit, facts) == field for other in rows):
            continue
        if field == "design_institution":
            if (evidence.get("reader") != "positioned_native_text"
                    or evidence.get("field") not in (None, "design_institution")):
                continue
        elif evidence.get("field") != field:
            continue
        recovered[row["id"]] = {"pending_id": row["id"], "unit_id": unit["id"],
            "original_text": row["text"], "original_bbox": {key: row.get(key) for key in ("x0", "y0", "x1", "y1")},
            "evidence": evidence, "image_sha256": (unit["metadata"]["local_sheet"])["image_sha"],
            "original_field": field, "status": "verified_by_independent_local_reading", "human_confirmation": False}
    return recovered
