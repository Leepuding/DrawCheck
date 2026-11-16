"""Live confirmation list. Historical rows and human decisions stay in storage."""
from collections import defaultdict
import math
import re

from backend.store.models import Source


_ANCHOR = ("unit_id", "text", "x0", "y0", "x1", "y1")
INVALID_LOCATION_REASON = "无有效识读位置，需补定位或重新识别"
_ITEM_PREFIX = re.compile(r"^(?:[（(][0-9]{1,3}[）)]|[0-9]{1,3}[、)）]|[0-9]{1,3}[.．](?!\s*[0-9]))\s*")
_EXPLICIT_RANGE_SEPARATOR = re.compile(r"(?<=-[0-9]{2})[ \t]*[~～][ \t]*(?=-[0-9]{2}\s*图)")
_EDGE_PUNCTUATION = "。.!！?？、，,；;：:（）()【】[]「」『』“”‘’"


def valid_pending_bbox(row):
    """An executable confirmation needs a finite, nonempty original-image box."""
    try:
        values = [row[key] for key in ("x0", "y0", "x1", "y1")]
        if any(isinstance(value, bool) for value in values):
            return False
        x0, y0, x1, y1 = map(float, values)
        return all(math.isfinite(value) for value in (x0, y0, x1, y1)) and (
            0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def invalid_open_location(row, units, *, unlocated_ids=()):
    return (row.get("status") == "待确认" and row.get("source") != Source.USER.value
            and (row.get("unit_id") not in units or not valid_pending_bbox(row)
                 or row.get("id") in unlocated_ids))


def unlocated_pending_ids(repo, version_id, *, rows=None, units=None):
    """A clamped/model-wide box is not a verified reading location.

    Mapping a crop can leave a positive, in-range rectangle at another line.
    Bind the diagnostic to the exact stored machine evidence; equal text at
    another physical position and processed human history stay independent.
    """
    rows = repo.list_records("pending_confirmations", version_id=version_id) if rows is None else rows
    units = ({u["id"]: u for u in repo.list_records("drawing_units", version_id=version_id)}
             if units is None else units)
    flagged = {(unit['id'], fid) for unit in units.values()
               for fid, annotation in ((unit.get("metadata") or {}).get("fact_annotations") or {}).items()
               if annotation.get("bbox_clamped") or annotation.get("region_bbox_unlocated")}
    if not flagged:
        return set()
    anchors = {(fact["unit_id"], fact["text"], *(fact[key] for key in ("x0", "y0", "x1", "y1")))
               for fact in repo.list_records("facts", version_id=version_id)
               if (fact['unit_id'], fact["id"]) in flagged and fact.get("source") != Source.USER.value}
    return {row["id"] for row in rows
            if row.get("status") == "待确认" and row.get("source") != Source.USER.value
            and (row["unit_id"], row["text"], *(row[key] for key in ("x0", "y0", "x1", "y1"))) in anchors}


def invalid_location_incomplete(repo, version_id, *, rows=None, units=None):
    """Page coverage for current questions; generic reading failures need no facts."""
    if rows is None:
        rows = repo.list_records("pending_confirmations", version_id=version_id)
    if not any(row.get("status") == "待确认" and row.get("source") != Source.USER.value for row in rows):
        return []
    if units is None:
        units = {u["id"]: u for u in repo.list_records("drawing_units", version_id=version_id)}
    unlocated = unlocated_pending_ids(repo, version_id, rows=rows, units=units)
    invalid = [row for row in rows if invalid_open_location(row, units, unlocated_ids=unlocated)]
    from backend.local_reading import local_reading_recoveries
    recovered = local_reading_recoveries(repo, version_id, rows=rows, units=units)
    invalid = [row for row in invalid if row["id"] not in recovered]
    grouped = defaultdict(list)
    # Invalid coordinates cannot revive an obsolete rule question. Only this subset
    # needs the existing rule-eligibility projection; ordinary valid rows stay cheap.
    for row in _current_questions(repo, version_id, invalid):
        grouped[row.get("unit_id")].append(row)
    return [{"step": "pending_location", "category": "incomplete", "unit_id": unit_id,
             "reason": f"{INVALID_LOCATION_REASON}（{len(group)}条）；识读内容：" + "；".join(
                 str(row.get("text") or "（空原文）") for row in group),
             "pending_ids": [row["id"] for row in group],
             "original_texts": [row.get("text") or "" for row in group]}
            for unit_id, group in grouped.items()]


def _anchor(row):
    return tuple(row[key] for key in _ANCHOR)


def _question_text(text):
    value = _ITEM_PREFIX.sub("", (text or "").strip(), count=1)
    # Keep interior punctuation, signed numbers and leading decimal points.
    # In particular a range separator, digit or negation must never disappear.
    while value and value[0] in _EDGE_PUNCTUATION and not re.match(r"^[.．][0-9]", value):
        value = value[1:].lstrip()
    return value.rstrip(_EDGE_PUNCTUATION + " \t\r\n\u3000")


def _overlap_ratio(a, b):
    ax, ay, ar, ab = (float(a[k]) for k in _ANCHOR[2:])
    bx, by, br, bb = (float(b[k]) for k in _ANCHOR[2:])
    intersection = max(0, min(ar, br)-max(ax, bx)) * max(0, min(ab, bb)-max(ay, by))
    union = (ar-ax)*(ab-ay) + (br-bx)*(bb-by) - intersection
    return intersection / union


def _deduplicate_machine_questions(rows):
    kept = defaultdict(list)
    for row in rows:
        if (row.get("status") == "待确认" and row.get("source") != Source.USER.value
                and valid_pending_bbox(row) and (text := _question_text(row.get("text")))):
            # The two tilde glyph widths denote the same signed drawing range.
            # Keep endpoint digits, other separators and both original records.
            if row["reason"].startswith("局部数字简写"):
                text = _EXPLICIT_RANGE_SEPARATOR.sub("～", text)
            group = kept[row["unit_id"], row["reason"], text]
            if any(_overlap_ratio(row, other) >= .5 for other in group):
                continue
            group.append(row)
        yield row


def _short_reference_decisions(repo, version_id, rows):
    from backend.evidence_selection import short_reference_questions
    from backend.references import AMBIGUOUS_REFERENCE_REASON

    processed = [row for row in rows if row["status"] in {"已确认", "无法确定"}
                 and row.get("source") == Source.VISION.value and row["reason"] == AMBIGUOUS_REFERENCE_REASON]
    if not processed:
        return {}
    units = {u["id"]: u for u in repo.list_records("drawing_units", version_id=version_id)}
    facts = repo.list_records("facts", version_id=version_id)
    bindings = short_reference_questions(units, facts, processed)
    decisions = {}
    for row in processed:
        fact = bindings.get(row["id"])
        if fact and (row["status"] == "无法确定"
                     or (decisions.get(fact["id"]) or {}).get("status") != "无法确定"):
            decisions[fact["id"]] = row
    return decisions


def _current_questions(repo, version_id, rows):
    from .title_source import resolved_catalog_questions
    resolved = resolved_catalog_questions(repo, version_id, rows=rows)
    rows = [row for row in rows if row["id"] not in resolved or row.get("source") == Source.USER.value
            or row.get("status") != "待确认"]
    from backend.references import AMBIGUOUS_REFERENCE_REASON, STRUCTURED_TARGET_CONFLICT_REASON

    if any(row["status"] == "待确认" and row["reason"] in
           {AMBIGUOUS_REFERENCE_REASON, STRUCTURED_TARGET_CONFLICT_REASON} for row in rows):
        decisions = _short_reference_decisions(repo, version_id, rows)
        originals = {_anchor(row) for row in decisions.values()}
        # The old rules could append a second pending from the projected user
        # text. Its source, reason and audit fact bind it to this same decision.
        positions = {(row["unit_id"], *(row[k] for k in _ANCHOR[2:])) for row in decisions.values()}
        previous_answers = {_anchor(fact) for fact in repo.list_records("facts", version_id=version_id)
                            if fact.get("source") == Source.USER.value
                            and (fact["unit_id"], *(fact[k] for k in _ANCHOR[2:])) in positions} if positions else set()
        superseded = {(AMBIGUOUS_REFERENCE_REASON, Source.VISION.value, anchor) for anchor in originals}
        superseded.update((STRUCTURED_TARGET_CONFLICT_REASON, Source.USER.value, anchor)
                          for anchor in previous_answers)
        rows = [row for row in rows if row["status"] != "待确认"
                or (row["reason"], row["source"], _anchor(row)) not in superseded]
    # Most projects have no rule-generated questions, so avoid loading facts and rule modules.
    if not any(p["status"] == "待确认" and p["reason"].startswith(
            ("两处同名", "未确认对应", "编号原文包含", "尺寸单位缺失",
             "缺少可核实的引用", "引用目标缺失", "局部数字简写", "引用对应多个", "引用候选图纸",
             "同卷册尺寸单位", "同卷册标高单位")) for p in rows):
        return rows

    from backend.evidence_selection import reliable_fact, selected_facts
    from backend.linking.service import SAME_NAME_PENDING_REASON, same_name_pairs
    from backend.references import (CROSS_PROJECT_VOLUME_REASON, SAME_VOLUME_RANGE,
                                    SAME_VOLUME_SHORTHAND, TITLE_CONFLICT_REASON, UNLISTED_REFERENCE_REASON,
                                    drawing_reference, resolve_reference, verified_sheet_number)
    from backend.rules.dimension import NO_UNIT_REASON
    from backend.rules.rules_b import (INVALID_NUMBER_REASON, NO_REFERENCE_REASON, OPEN_SCOPE_REASON,
                                       UNPAIRED_REASON, _same_object, _visible_number, category,
                                       object_review_candidates, visible_detail_hint)
    from backend.rules.unit_declaration import unit_conflict_reason, volume_units

    unit_reasons = {NO_UNIT_REASON, unit_conflict_reason("尺寸"), unit_conflict_reason("标高")}
    specific_evidence_reasons = {TITLE_CONFLICT_REASON, CROSS_PROJECT_VOLUME_REASON, UNLISTED_REFERENCE_REASON}
    short_specific_reasons = {"引用对应多个图纸身份，需确认", "引用候选图纸身份尚未核实，需确认",
                              *specific_evidence_reasons}
    managed = {SAME_NAME_PENDING_REASON, UNPAIRED_REASON, INVALID_NUMBER_REASON, NO_UNIT_REASON,
               NO_REFERENCE_REASON, OPEN_SCOPE_REASON,
               AMBIGUOUS_REFERENCE_REASON} | unit_reasons | short_specific_reasons
    affected = {p["reason"] for p in rows if p["status"] == "待确认" and p["reason"] in managed}
    if not affected:
        return rows

    units, facts = selected_facts(repo, version_id)
    active = defaultdict(set)
    if SAME_NAME_PENDING_REASON in affected:
        active[SAME_NAME_PENDING_REASON].update(_anchor(left) for left, _ in same_name_pairs(facts))
    conflict_evidence = {}
    if affected & (unit_reasons - {NO_UNIT_REASON}):
        for reason, evidence in volume_units(repo, version_id, units=units.values(), facts=facts).conflicts():
            first = evidence[0]
            anchor = _anchor({**first, **first["bbox"]})
            active[reason].add(anchor)
            conflict_evidence[reason, anchor] = evidence
    for cat, reason in (("材料规格", UNPAIRED_REASON), ("数量与材料表", UNPAIRED_REASON),
                        ("编号写法", INVALID_NUMBER_REASON)):
        if reason not in affected:
            continue
        group = [f for f in facts if category(f) == cat]
        candidates = object_review_candidates(group)
        for fact in group:
            if fact["id"] not in candidates:
                continue
            if reason == INVALID_NUMBER_REASON:
                if _visible_number(fact)["literal"] is not None:
                    continue
            elif any(other["id"] != fact["id"] and _same_object(fact, other)
                     and (not fact.get("comparison_key") or not other.get("comparison_key") or
                          fact["comparison_key"] == other["comparison_key"]) for other in group):
                continue
            active[reason].add(_anchor(fact))

    by_anchor = defaultdict(list)
    reference_reasons = {NO_REFERENCE_REASON, OPEN_SCOPE_REASON, AMBIGUOUS_REFERENCE_REASON}
    short_refs = defaultdict(list)
    if affected & (reference_reasons | short_specific_reasons):
        homes = {uid: verified_sheet_number(unit) for uid, unit in units.items()}
        for fact in facts:
            if category(fact) == "详图索引断链":
                anchor = _anchor(fact)
                by_anchor[anchor].append(fact)
                if reliable_fact(fact):
                    reference = drawing_reference(fact, home_number=homes.get(fact["unit_id"]))
                    if reference and (reference.get("home_number") or reference.get("target")):
                        resolved = resolve_reference(reference, units)
                        if reference.get("home_number") or resolved.get("reason") == TITLE_CONFLICT_REASON:
                            short_refs[anchor].append(resolved)

    def newer_reference_reason(reason):
        return (reason in specific_evidence_reasons or reason in {OPEN_SCOPE_REASON, AMBIGUOUS_REFERENCE_REASON,
                           STRUCTURED_TARGET_CONFLICT_REASON, "引用对应多个图纸身份，需确认",
                           "引用候选图纸身份尚未核实，需确认", "详图索引看不清，待确认"}
                or (reason.startswith("目标图 ") and " 的详图 " in reason
                    and reason.endswith(" 未核实，待确认")))

    # A parser upgrade may expose a target and produce a more specific question.
    # Supersede only an open machine fallback at the exact same physical anchor;
    # leave its stored history, lone old questions and all human decisions intact.
    newer_reference_anchors = {_anchor(row) for row in rows
        if row["status"] == "待确认" and row.get("source") != Source.USER.value
        and newer_reference_reason(row["reason"])
        and any(drawing_reference(fact) or visible_detail_hint(fact)
                for fact in by_anchor[_anchor(row)])}

    specific_anchors = {(_anchor(row), row["reason"]) for row in rows
                        if row.get("status") == "待确认" and row.get("source") != Source.USER.value
                        and row["reason"] in specific_evidence_reasons
                        and any(ref.get("reason") == row["reason"] for ref in short_refs[_anchor(row)])}
    specific_anchors_with_replacement = {anchor for anchor, _ in specific_anchors}
    precise_short_anchors = {_anchor(row) for row in rows
        if row["status"] == "待确认" and row.get("source") != Source.USER.value
        and row["reason"] != AMBIGUOUS_REFERENCE_REASON and newer_reference_reason(row["reason"])
        and _anchor(row) in short_refs}
    missing_facts = {ref["fact_id"] for refs in short_refs.values() for ref in refs if ref["status"] == "missing"}
    broken_ids = set()
    if missing_facts and AMBIGUOUS_REFERENCE_REASON in affected:
        for issue in repo.list_records("issues", version_id=version_id):
            rule = issue.get("rule_result") or {}
            if rule.get("fixture_id") == "ISSUE-INDEX-BROKEN" and rule.get("reference_target"):
                broken_ids.update(rule.get("fact_ids") or [])
    current = []
    for row in rows:
        reason = row["reason"]
        if row["status"] == "待确认" and row.get("source") != Source.USER.value and reason in affected:
            anchor = _anchor(row)
            if reason in reference_reasons | short_specific_reasons:
                short = short_refs.get(anchor, [])
                if short and all(ref["status"] == "resolved" for ref in short):
                    continue
                if reason in specific_evidence_reasons and (anchor, reason) not in specific_anchors:
                    continue
                if reason in short_specific_reasons and not short:
                    current.append(row)  # Legacy full-code path keeps its behavior.
                    continue
                if reason in {OPEN_SCOPE_REASON, AMBIGUOUS_REFERENCE_REASON} and anchor in specific_anchors_with_replacement:
                    continue
                if reason == NO_REFERENCE_REASON and anchor in newer_reference_anchors:
                    continue
                if reason == AMBIGUOUS_REFERENCE_REASON and (anchor in precise_short_anchors or
                        (short and all(ref["fact_id"] in broken_ids for ref in short))):
                    continue
                # Without a literal sheet reference, a model-supplied equipment code cannot
                # require reference confirmation. Keep explicit internal detail concerns.
                if not any(drawing_reference(fact) or (reason == NO_REFERENCE_REASON and visible_detail_hint(fact))
                           for fact in by_anchor[anchor]):
                    continue
                if len(targets := {ref["target"] for ref in short if ref.get("target")
                                   and ref.get("target_basis") == SAME_VOLUME_SHORTHAND}) == 1:
                    row = {**row, "reference_target": next(iter(targets))}
            elif anchor not in active[reason]:
                continue
            elif evidence := conflict_evidence.get((reason, anchor)):
                row = {**row, "unit_declarations": evidence}
        current.append(row)
    return current


def current_pending(repo, version_id):
    rows = _current_questions(repo, version_id, repo.list_records("pending_confirmations", version_id=version_id))
    if any(row.get("status") == "待确认" and row.get("source") != Source.USER.value for row in rows):
        from backend.local_reading import corroborated_prose_questions

        units = {u["id"]: u for u in repo.list_records("drawing_units", version_id=version_id)}
        unlocated = unlocated_pending_ids(repo, version_id, rows=rows, units=units)
        rows = [row for row in rows if not invalid_open_location(row, units, unlocated_ids=unlocated)]
        retired = corroborated_prose_questions(repo, version_id, rows=rows, units=units)
        rows = [row for row in rows if row["id"] not in retired]
    return list(_deduplicate_machine_questions(rows))


def current_incomplete(repo, version_id, rows):
    """Keep genuine coverage failures while retiring obsolete sheet-reference diagnostics."""
    from backend.evidence_selection import position_image_changed, reliable_fact, selected_facts
    from backend.references import (_independent_title, SAME_VOLUME_SHORTHAND, absence_scope,
                                    drawing_reference, import_scope_diagnostics, resolve_reference,
                                    verified_sheet_number)
    from backend.rules.catalog_titleblock import CATALOG_SCOPE_INCOMPLETE, TITLE_UNVERIFIED

    # Rebuild this live projection so repeated reads cannot append duplicate diagnostics.
    rows = [row for row in rows if row.get("step") != "pending_location"]
    pending = repo.list_records("pending_confirmations", version_id=version_id)
    rows += invalid_location_incomplete(repo, version_id, rows=pending)
    decisions = _short_reference_decisions(repo, version_id, pending)
    if decisions:
        units, effective = selected_facts(repo, version_id)
        references = {}
        for item in effective:
            if item["id"] in decisions and item.get("confirmed_short_reference"):
                reference = drawing_reference(item, home_number=verified_sheet_number(units[item["unit_id"]]))
                references[item["id"]] = resolve_reference(reference, units)
        obsolete = references.keys() | {fid for fid, decision in decisions.items() if decision["status"] == "无法确定"}
        rows = [row for row in rows if row.get("step") not in {"link", "rules_b"} or row.get("fact_id") not in obsolete]
        # An explicit but unverified target remains live coverage after the old
        # ambiguity has been answered. Unsure is already counted by its pending.
        rows.extend({"step": "link", "category": "incomplete", "unit_id": decisions[fid]["unit_id"],
                     "fact_id": fid, "reason": reference["reason"]}
                    for fid, reference in references.items() if reference["status"] != "resolved")
    reference_rows = {r["fact_id"] for r in rows if r.get("fact_id") and r.get("step") in {"link", "rules_b"}}
    catalog_rows = [r for r in rows if r.get("step") == "rules_a"
                    and (r.get("reason") == TITLE_UNVERIFIED or str(r.get("reason")).startswith(CATALOG_SCOPE_INCOMPLETE))]
    if not reference_rows and not catalog_rows:
        return rows
    units = {u["id"]: u for u in repo.list_records("drawing_units", version_id=version_id)}
    facts = repo.list_records("facts", version_id=version_id)
    invalid = set()
    for fact in facts:
        if fact["id"] not in reference_rows:
            continue
        unit = units[fact["unit_id"]]
        meta = unit.get("metadata") or {}
        annotation = (meta.get("fact_annotations") or {}).get(fact["id"], {})
        if position_image_changed(annotation, meta.get("extract_image_sha256")):
            invalid.add(fact["id"])
            continue
        item = {**fact, **annotation}
        reference = drawing_reference(item, home_number=verified_sheet_number(unit))
        if reference is None or (reference.get("target_basis") == SAME_VOLUME_SHORTHAND
                and reliable_fact(item) and not item.get("region_bbox_unlocated")
                and resolve_reference(reference, units)["status"] == "resolved"):
            invalid.add(fact["id"])
    current = []
    scope = None
    for row in rows:
        if row.get("step") in {"link", "rules_b"} and row.get("fact_id") in invalid:
            continue
        if row in catalog_rows:
            unit = units.get(row.get("unit_id"))
            if row["reason"] == TITLE_UNVERIFIED and unit and _independent_title(unit, facts):
                continue
            if row["reason"].startswith(CATALOG_SCOPE_INCOMPLETE):
                if scope is None:
                    scope = absence_scope(units, facts, import_diagnostics=import_scope_diagnostics(repo, version_id))
                if scope["closed"]:
                    continue
                row = {**row, "reason": CATALOG_SCOPE_INCOMPLETE + "；".join(scope["reasons"])}
        current.append(row)
    return current
