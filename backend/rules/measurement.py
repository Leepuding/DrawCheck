"""Tolerance checks for already paired dimensions and elevations."""
from decimal import Decimal

from backend.evidence_selection import selected_facts
from .common import (classify_fact, comparable_pairs, compared_object, comparison_row,
                     evidence_from_fact, exceeds_tolerance, load_tolerances,
                     visible_measurement_values, write_issue)
from .unit_declaration import comparison_declarations, unverified_units_reason, volume_units


def run_measurement(store, version_id, category, *, task_id=None, settings=None,
                    persist=True, comparison_facts=None, declarations=None):
    elevation = category == "标高"
    unit = "m" if elevation else "mm"
    tolerance_key = f"tolerance_{unit}"
    field = "elevation" if elevation else "dimension"
    tolerance = load_tolerances(store, task_id, settings)[f"{field}_{tolerance_key}"]
    facts = selected_facts(store, version_id)[1] if comparison_facts is None else comparison_facts
    if declarations is None:
        declarations = volume_units(store, version_id, facts=facts)
    issues, comparisons, incomplete = [], [], {}
    for left, right in comparable_pairs([f for f in facts if classify_fact(f) == category]):
        label = compared_object(left, right, category)
        record_pair = comparison_facts is not None
        a, b = (declarations.for_fact(fact, category) for fact in (left, right))
        unit_evidence = comparison_declarations(a, b)
        lv, rv = (visible_measurement_values(fact) for fact in (left, right))
        valid_left = bool(lv) and (not elevation or len(lv) == 1)
        valid_right = bool(rv) and (not elevation or len(rv) == 1)
        if not valid_left or not valid_right or a.scale is None or b.scale is None:
            reason = (f"{category}数值未能读全" if not valid_left or not valid_right else
                      unverified_units_reason(category, a, b))
            if record_pair:
                comparisons.append(comparison_row(left, right, category=category, label=label,
                    outcome="unverified", basis=reason, unit_declarations=unit_evidence))
            if comparison_facts is None and not (a.conflict or b.conflict):
                uid = (left if not valid_left or a.scale is None else right)["unit_id"]
                incomplete[uid, reason] = {"unit_id": uid, "reason": reason, "category": "incomplete"}
            continue
        diff = (max(abs(x*a.scale - y*b.scale) for x, y in zip(lv, rv)) / (1000 if elevation else 1)
                if len(lv) == len(rv) else None)
        mismatch = diff is None or exceeds_tolerance(diff, tolerance)
        if record_pair:
            summary = (f"相差 {diff} {unit}，容差 {tolerance:g} {unit}" if diff is not None else "两边标注的尺寸分量数量不同")
            comparisons.append(comparison_row(left, right, category=category, label=label,
                outcome="mismatch" if mismatch else "match", basis=summary, unit_declarations=unit_evidence))
        if not mismatch:
            continue
        title = f"{category}不一致：{left['text']} 对 {right['text']}"
        if elevation:
            fixture = "ISSUE-ELV-CONFLICT" if diff >= Decimal("0.05") else "ISSUE-ELV-TOL-OVER"
        else:
            fixture = "ISSUE-DIM-TOL-OVER" if len(lv) == len(rv) == 1 else "ISSUE-DIM-CONFLICT"
        if persist:
            issues.append(write_issue(
                store, version_id, title=title, left=evidence_from_fact(left), right=evidence_from_fact(right),
                category=category, left_value=left["text"], right_value=right["text"],
                basis=f"按已确认的同一对象或标注项目「{label}」配对。", fixture_id=fixture,
                extra={tolerance_key: tolerance, "object_label": label, "unit_declarations": unit_evidence,
                       "abs_diff" if elevation else "max_abs_diff": str(diff) if diff is not None else None,
                       "component_counts": [len(lv), len(rv)]},
            ))
        else:
            issues.append({"title": title, "category": category, "fixture_id": fixture})
    return {"issues": issues, "comparisons": comparisons, "incomplete": list(incomplete.values()),
            "pendings": [], tolerance_key: tolerance}
