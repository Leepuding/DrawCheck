"""Current usable facts, with human decisions applied before machine-state guards.

The stored facts remain an audit trail. A failed recognition attempt must not
promote its prior machine output into current evidence for rules or linking.
"""
from collections import defaultdict
import math

from backend.store import Source
from backend.title_identity import has_manual_number


_BOX_KEYS = ("x0", "y0", "x1", "y1")


def _anchor(row):
    return (row["unit_id"], *(row[k] for k in _BOX_KEYS))


def position_image_changed(annotation, image_sha256):
    previous_sha = (annotation.get("position_recovery") or {}).get("image_sha256")
    return bool(previous_sha and previous_sha != image_sha256)


def short_reference_questions(units, stored, pending):
    """Map target questions to the independently read fact at their exact anchor."""
    from backend.references import AMBIGUOUS_REFERENCE_REASON

    questions = defaultdict(list)
    for row in pending:
        if row.get("reason") == AMBIGUOUS_REFERENCE_REASON and row.get("source") == Source.VISION.value:
            questions[_anchor(row), row["text"]].append(row)
    if not questions:
        return {}
    candidates = defaultdict(list)
    for fact in stored:
        key = (_anchor(fact), fact["text"])
        if key not in questions or fact.get("source") != Source.VISION.value:
            continue
        meta = (units.get(fact["unit_id"]) or {}).get("metadata") or {}
        annotation = (meta.get("fact_annotations") or {}).get(fact["id"], {})
        proof = annotation.get("position_recovery") or {}
        if proof.get("recovery_stage") == "corroborated_short_reference":
            candidates[key].append((fact, meta, annotation, proof))
    bindings = {}
    for key, group in candidates.items():
        if len(group) != 1:
            continue
        fact, meta, annotation, proof = group[0]
        locations = proof.get("locations") or []
        if (meta.get("extract_status") == "ok" and not meta.get("out_of_scope")
                and not annotation.get("bbox_clamped") and not annotation.get("region_bbox_unlocated")
                and proof.get("scope") == "drawing_reference" and proof.get("status") == "unique_exact"
                and proof.get("image_sha256") and proof["image_sha256"] == meta.get("extract_image_sha256")
                and proof.get("recovered_text") == fact["text"]
                and isinstance(locations, list) and len(locations) == 1 and isinstance(locations[0], dict)
                and locations[0].get("match_type") == "short_reference_reread"
                and locations[0].get("literal_occurrence_count") == 1
                and locations[0].get("bbox") == {k: fact[k] for k in _BOX_KEYS}):
            bindings.update((row["id"], fact) for row in questions[key])
    return bindings


def _relocated_claim_key(fact, *, human_anchor=False):
    """Bind a repeated claim only through its explicit object and exact field.

    At a human anchor, unclear/bbox_clamped describe the old reading/location,
    not permission for a new reading to override that decision. Its identity
    must still be explicit, specific and supported by a reliable confidence.
    """
    if (fact.get("identity_confirmed") is not True or fact.get("region_conflict")
            or isinstance(fact.get("confidence", 1), bool)
            or not reliable_fact({**fact, "unclear": False} if human_anchor else fact)):
        return None
    category = fact.get("category")
    object_id, field = fact.get("object_id") or "", fact.get("comparison_key") or ""
    if not all(isinstance(value, str) for value in (category, object_id, field)):
        return None
    if not category.strip() or not field.strip():
        return None
    # The comparison rules deliberately reject same-sheet material-table
    # identity: an object/model name does not bind two different physical rows.
    if fact.get("quantity_role") == "material_table":
        return None
    scope = tuple(fact.get(key) for key in (
        "unit", "quantity_role", "target_drawing_number", "target_detail_number", "detail_number"))
    if any(value is not None and not isinstance(value, str) for value in scope):
        return None
    # Reuse the rules' identity threshold, without importing rules on the common
    # path where no processed human questions exist.
    from backend.rules.rules_b import _specific_identity
    if not _specific_identity(object_id if object_id else field):
        return None
    return (fact["unit_id"], category, object_id, field, fact.get("text"), *scope)


def selected_facts(store, version_id):
    units = {u["id"]: u for u in store.list_records("drawing_units", version_id=version_id)}
    pending = store.list_records("pending_confirmations", version_id=version_id)
    stored = store.list_records("facts", version_id=version_id)
    facts = []

    pending_at = defaultdict(list)
    for row in pending:
        pending_at[_anchor(row), row["text"]].append(row)
    machine_anchors = {(_anchor(fact), fact["text"]) for fact in stored if fact["source"] != Source.USER.value}
    audit_locations = {location for location, text in pending_at if (location, text) in machine_anchors}
    short_questions = short_reference_questions(units, stored, pending)
    stale_human_anchors = set()
    relocation_candidates = []
    if any(row["status"] in {"已确认", "无法确定"} for row in pending):
        for original in stored:
            if original.get("source") == Source.USER.value:
                continue
            anchor = (_anchor(original), original["text"])
            matches = pending_at[anchor]
            if not any(row["status"] in {"已确认", "无法确定"} for row in matches):
                continue
            metadata = (units.get(original["unit_id"]) or {}).get("metadata") or {}
            annotation = (metadata.get("fact_annotations") or {}).get(original["id"], {})
            if position_image_changed(annotation, metadata.get("extract_image_sha256")):
                stale_human_anchors.add(anchor)
                continue
            if (annotation.get("position_recovery") or {}).get("recovery_stage") == "corroborated_short_reference":
                continue
            relocation_candidates.append((anchor, {**original, **annotation}))
    human_claims = {key for anchor, candidate in relocation_candidates if anchor not in stale_human_anchors
                    and (key := _relocated_claim_key(candidate, human_anchor=True)) is not None}

    for fact in stored:
        meta = units[fact["unit_id"]].get("metadata") or {}
        if meta.get("out_of_scope"):
            continue
        # Appended confirmation facts are audit records when their original
        # anchor is still present. Standalone USER facts remain independently valid.
        if fact.get("source") == Source.USER.value and _anchor(fact) in audit_locations:
            continue
        annotation = (meta.get("fact_annotations") or {}).get(fact["id"], {})
        if position_image_changed(annotation, meta.get("extract_image_sha256")):
            continue
        item = {**fact, **annotation}
        anchor = (_anchor(fact), fact["text"])
        matches = pending_at[anchor]
        proof = annotation.get("position_recovery") or {}
        is_short = proof.get("recovery_stage") == "corroborated_short_reference"
        if anchor in stale_human_anchors:
            matches = [p for p in matches if p["status"] == "待确认"]
        else:
            matches = [p for p in matches if p["status"] == "待确认"
                       or (short_questions.get(p["id"]) or {}).get("id") == fact["id"]
                       or (not is_short and p["id"] not in short_questions)]
        if any(p["status"] == "无法确定" for p in matches):
            continue
        confirmed = [p for p in matches if p["status"] == "已确认"]
        confirmed_text = None
        if confirmed:
            confirmed_text = confirmed[-1]["confirmed_text"]
            if is_short:
                from backend.references import short_reference_answer

                if confirmed_text := short_reference_answer(confirmed_text):
                    # The original model fields stay in storage with the recovered literal.
                    item.pop("target_drawing_number", None)
                    item.pop("target_detail_number", None)
                    item["confirmed_short_reference"] = True
            if confirmed_text:
                item.update(text=confirmed_text, source=Source.USER.value, confirmed_from=fact["source"],
                            numeric_value=None, confidence=1.0, unclear=False)
        if not confirmed_text:
            if any(p["status"] == "待确认" and p["reason"] != "尺寸单位缺失或看不清，待确认" and any(k in p["reason"] for k in ("看不清", "把握度", "读数不一致")) for p in matches):
                continue
            if item.get("positioned_reading_only") and item.get("source") == Source.VISION.value:
                # The local proof answers where the literal is. It does not turn
                # the original uncertain model fact into comparison evidence.
                continue
        if meta.get("extract_status") == "incomplete" and item.get("source") == Source.VISION.value:
            continue
        if item.get("region_bbox_unlocated") and item.get("source") != Source.USER.value:
            # A readable literal without a unique physical anchor stays audit
            # evidence. It cannot establish a current reference/comparison.
            continue
        if (human_claims and item.get("source") != Source.USER.value
                and _relocated_claim_key(item) in human_claims):
            # Keep the new machine fact/position in storage as audit evidence.
            # Never copy a human correction to that newly observed location.
            continue
        facts.append(item)
    return units, facts


def reliable_fact(fact):
    """Uncertain machine text cannot create an identity association."""
    if fact.get("unclear"):
        return False
    try:
        confidence = float(fact.get("confidence", 1))
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(confidence) and .55 <= confidence <= 1


def usable_title_identity(unit):
    meta = unit.get("metadata") or {}
    if meta.get("out_of_scope"):
        return False
    if has_manual_number(unit):
        return bool((unit.get("drawing_number") or "").strip())
    return meta.get("extract_status") != "incomplete" and not meta.get("title_identity_uncertain") and not meta.get("no_title_block")
