"""Revalidate a catalogue's actual native field before retiring its old question."""
from backend.store import Source

TITLE_SOURCE_REASON = "图签原文来源或候选位置需确认"


def resolved_catalog_questions(repo, version_id, *, rows=None):
    history = repo.list_records("pending_confirmations", version_id=version_id)
    candidates = [row for row in (history if rows is None else rows)
                  if row.get("version_id") == version_id and row.get("status") == "待确认"
                  and row.get("reason") == TITLE_SOURCE_REASON
                  and row.get("source") in {Source.CAD_TEXT.value, Source.VISION.value}]
    if not candidates:
        return {}
    from backend.catalog.title_resolver import resolve_title_number
    from backend.evidence_selection import reliable_fact, selected_facts
    units, selected = selected_facts(repo, version_id)
    facts = [fact for fact in selected if fact.get("source") == Source.CAD_TEXT.value and reliable_fact(fact)]
    result = {}
    for row in candidates:
        unit = units.get(row.get("unit_id"))
        if not unit or unit.get("version_id") != version_id:
            continue
        meta = unit.get("metadata") or {}
        saved = meta.get("title_number_resolution") or {}
        if (not meta.get("is_catalog_sheet") or meta.get("identity_kind") != "catalog_volume"
                or meta.get("title_identity_uncertain") is not False
                or saved.get("identity_kind") != "catalog_volume"
                or row.get("text") != unit.get("drawing_number")):
            continue
        # An existing decision about this title field remains authoritative even
        # when its original model text or large model box was different.
        if any(other.get("unit_id") == unit["id"] and other.get("reason") == TITLE_SOURCE_REASON
               and other.get("status") in {"已确认", "无法确定"} for other in history):
            continue
        current = resolve_title_number(unit, {"drawing_number": saved.get("vision_drawing_number", ""),
            "bbox": meta.get("title_bbox")}, facts)
        if (current.get("status") != "cad_selected" or current.get("identity_kind") != "catalog_volume"
                or current.get("selected_source") != Source.CAD_TEXT.value
                or current.get("requires_confirmation") is not False
                or current.get("selected_drawing_number") != row["text"]):
            continue
        indexed = {fact["id"]:fact for fact in facts if fact["unit_id"] == unit["id"]}
        evidence = {}
        for key in ("selected_evidence", "label_evidence", "heading_evidence"):
            value = current.get(key)
            if not isinstance(value, dict) or value != saved.get(key):
                break
            fact = indexed.get(value.get("fact_id"))
            if (not fact or fact.get("version_id") != version_id or value.get("source") != fact["source"]
                    or value.get("text") != fact["text"]
                    or value.get("bbox") != {k:fact[k] for k in ("x0", "y0", "x1", "y1")}):
                break
            evidence[key] = value
        else:
            result[row["id"]] = {"pending_id":row["id"], "unit_id":unit["id"],
                "original_text":row["text"], "identity_kind":"catalog_volume",
                "status":"verified_by_native_catalog_field", "human_confirmation":False, **evidence}
    return result
