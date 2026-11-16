"""Compare displayed dimensions at the same position in copies of a CAD base."""
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from itertools import combinations

from backend.store import BBox, Evidence, Source

from .common import load_tolerances, write_issue
from .unit_declaration import comparison_declarations, unverified_units_reason, volume_units


_BOX_KEYS = ("x0", "y0", "x1", "y1")


def _location(item):
    box = (item.get("evidence") or {}).get("bbox") or {}
    try:
        values = tuple(float(box[key]) for key in _BOX_KEYS)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    x0, y0, x1, y1 = values
    return values if 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1 else None


def _manual_overlap(box, decisions):
    for row in decisions:
        other = tuple(row[key] for key in _BOX_KEYS)
        if box[0] < other[2] and other[0] < box[2] and box[1] < other[3] and other[1] < box[3]:
            return True
    return False


def _groups(store, version_id, units):
    candidates = []
    for unit in units:
        meta = unit.get("metadata") or {}
        if unit.get("kind") != "dwg_frame" or meta.get("out_of_scope") or not meta.get("preview_relative_path"):
            continue
        for item in meta.get("native_measurements") or []:
            if item.get("shared_block_mark"):
                candidates.append((unit, item))
    if not candidates:
        return {}

    originals = {row["id"]: row["sha256"] for row in store.list_records("original_files", version_id=version_id)}
    manual = defaultdict(list)
    for row in store.list_records("pending_confirmations", version_id=version_id):
        if row.get("status") in {"已确认", "无法确定"} and all(isinstance(row.get(k), (int, float)) for k in _BOX_KEYS):
            manual[row["unit_id"]].append(row)

    groups = defaultdict(lambda: defaultdict(list))
    for unit, item in candidates:
        digest = originals.get(unit["original_id"])
        mark = item["shared_block_mark"]
        if not digest or not all(mark.get(key) for key in ("block", "line_basis_sha256", "key")):
            continue
        ev = item.get("evidence") or {}
        box = _location(item)
        if not box or ev.get("source") != Source.CAD_TEXT.value or not ev.get("text"):
            continue
        try:
            value = Decimal(item["value"])
        except (KeyError, InvalidOperation, TypeError, ValueError):
            continue
        if not value.is_finite() or value < 0:
            continue
        key = mark["block"].casefold(), mark["line_basis_sha256"], mark["key"]
        groups[key][digest].append((unit["id"], item, value, _manual_overlap(box, manual[unit["id"]])))
    return groups


def run_dwg_dimensions(store, version_id, *, units, task_id=None, settings=None, persist=True, declarations=None):
    tolerance = Decimal(str(load_tolerances(store, task_id, settings)["dimension_tolerance_mm"]))
    units = list(units)
    if declarations is None:
        declarations = volume_units(store, version_id, units=units)
    comparisons, issues = [], []
    positions = 0
    for sources in _groups(store, version_id, units).values():
        # Require one mark per source file. Identical file bytes count as one
        # source, and an ambiguous source cannot establish an independent side.
        candidates = sorted((rows[0] for rows in sources.values() if len(rows) == 1), key=lambda row: row[0])
        if len(candidates) < 2:
            continue
        positions += 1
        unverified_values = set()
        for (left_id, left, a, manual_a), (right_id, right, b, manual_b) in combinations(candidates, 2):
            use_a, use_b = (declarations.for_fact({**item, "unit_id": uid, "text": item["evidence"]["text"]},
                "尺寸", positioned_value=item["value"]) for uid, item in ((left_id, left), (right_id, right)))
            scale_a, scale_b = use_a.scale, use_b.scale
            unit_evidence = comparison_declarations(use_a, use_b) if not (manual_a or manual_b) else []
            proof = {"left": left["shared_block_mark"], "right": right["shared_block_mark"]}
            left_value = f"{left['value']} {left.get('unit', '')}".strip()
            right_value = f"{right['value']} {right.get('unit', '')}".strip()
            label = f"底图标注 {positions}"
            if manual_a or manual_b:
                outcome, basis = "unverified", "这处标注已有人工处理，自动读数仅供参考。"
            elif scale_a is None or scale_b is None:
                readings = (f"两张图均标注 {left_value}" if left_value == right_value else
                            f"两张图分别标注 {left_value} 和 {right_value}")
                outcome = "unverified"
                if use_a.conflict or use_b.conflict:
                    basis = f"{readings}。{unverified_units_reason('尺寸', use_a, use_b)}。"
                else:
                    unit_note = "两处均未标单位" if scale_a is None and scale_b is None else "其中一处未标单位"
                    basis = f"{readings}。{unit_note}且无可用声明，实际尺寸待核对。"
                # Multiple copies with the same readings do not create
                # multiple unresolved items for the same mark and reason.
                signature = (tuple(sorted((left_value, right_value))),
                             tuple(sorted((e["volume"], e["unit"]) for e in unit_evidence)))
                if signature in unverified_values:
                    continue
                unverified_values.add(signature)
            else:
                diff = abs(a * scale_a - b * scale_b)
                outcome = "mismatch" if diff > tolerance else "match"
                basis = f"两张图引用相同底图的同一标注位置，读数相差 {diff} mm，容差 {tolerance} mm"
            comparisons.append({
                "category": "尺寸", "label": label, "outcome": outcome, "basis": basis,
                "left_unit_id": left_id, "right_unit_id": right_id, "left_value": left_value, "right_value": right_value,
                "left_evidence": left["evidence"], "right_evidence": right["evidence"],
                "shared_block_mark": proof, "source_file_count": len(candidates), "unit_declarations": unit_evidence,
            })
            if outcome != "mismatch":
                continue
            title = f"共用底图尺寸标注不一致（{left_value} / {right_value}）"
            if persist:
                def evidence(uid, item):
                    source = item["evidence"]
                    return Evidence(uid, Source.CAD_TEXT, source["text"], BBox(**source["bbox"]))
                issues.append(write_issue(
                    store, version_id, title=title, left=evidence(left_id, left), right=evidence(right_id, right),
                    category="尺寸", left_value=left_value, right_value=right_value, basis=basis,
                    extra={"match_basis": "dwg_shared_block_mark", "shared_block_mark": proof,
                           "max_abs_diff": str(diff), "tolerance_mm": float(tolerance),
                           "unit_declarations": unit_evidence},
                ))
            else:
                issues.append({"category": "尺寸", "title": title, "basis": basis})
    return {"comparisons": comparisons, "issues": issues, "positions": positions}
