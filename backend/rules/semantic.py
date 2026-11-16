"""Fuse separately sourced semantic and vision records before local cross-sheet checks."""
from collections import defaultdict
from decimal import InvalidOperation
from itertools import product
import re

from backend.evidence_selection import reliable_fact, selected_facts
from backend.extract.semantic import _COUNT, _TAG, _noun, _valid_anchor, amount, asset_tags, compact, comparable_quantities, quantity_note_candidates
from backend.extract.semantic_job import active_records
from backend.extract.engineering import cable_specification, canonical_cable_spec, table_cable_specification
from backend.store import Source

from .common import comparison_row, evidence_from_fact, write_issue


def _primary(record):
    cells = record["cells"]
    if record["kind"] == "material_spec":
        return cells.get("statement") or cells.get("visual_spec") or cells["spec"]
    return cells.get("label") or cells.get("statement") or cells["value"]


def _overlap(a, b):
    return all(a[k] < b[j] and b[k] < a[j] for k, j in (("x0", "x1"), ("y0", "y1")))


def _same_count(count, unit, record):
    unit_names = {"m": "米"}
    try:
        return amount(str(count)) == amount(record["value"]) and unit_names.get(unit, unit) == unit_names.get(record["unit"], record["unit"])
    except (InvalidOperation, TypeError):
        return False


def _vision_readings(record, visual):
    primary = _primary(record)
    support, conflicts = [], []
    for fact in visual:
        if record["unit_id"] != fact["unit_id"] or not reliable_fact(fact):
            continue
        human = fact["source"] == Source.USER.value
        if primary["source"] == Source.USER.value and not human:
            continue
        text = compact(fact["text"])
        original = compact(fact.get("original_visual_text", fact["text"]))
        manual_changed = fact.get("manual_unsure") or (human and original != text)
        if record["kind"] == "asset":
            if not _overlap(primary, fact):
                continue
            tags = _TAG.findall(text)
            if manual_changed and record["tag"] in _TAG.findall(original):
                conflicts.append(fact)
            elif record["tag"] in tags:
                support.append(fact)
            elif record["role"] == "overview" and _TAG.fullmatch(text):
                conflicts.append(fact)
        elif record["kind"] == "quantity":
            name = record["object"]
            if record["role"] == "material_table":
                serial = compact(record["cells"]["serial"]["text"])
                visual_original = fact.get("original_visual_text", fact["text"])
                match = re.match(r"^\s*(\d{1,4})\s+", visual_original)
                if not match or match[1] != serial or not any(noun in compact(visual_original) for noun in (name, record["name"])):
                    continue
                if manual_changed:
                    conflicts.append(fact)
                    continue
                count, unit = fact.get("numeric_value"), fact.get("unit")
            else:
                if not _overlap(primary, fact) or fact.get("category") not in {"数量与材料表", "数量"}:
                    continue
                if not any(noun in original for noun in (name, record["name"], "组件" if name == "光伏组件" else name)):
                    continue
                if manual_changed:
                    conflicts.append(fact)
                    continue
                hits = list(_COUNT.finditer(text))
                if len(hits) != 1:
                    continue
                count, unit = hits[0]["value"], hits[0]["unit"]
            if count is not None and unit:
                target = support if _same_count(count, unit, record) else conflicts
                target.append(fact)
        elif record["kind"] == "material_spec" and manual_changed:
            serial = record["cells"].get("serial")
            same_row = serial and re.match(r"^\s*" + re.escape(compact(serial["text"])) + r"\s+", fact.get("original_visual_text", ""))
            if _overlap(primary, fact) or same_row:
                conflicts.append(fact)
    return support, conflicts


def _audit(fact):
    return {"fact_id": fact["id"], "unit_id": fact["unit_id"], "text": fact["text"], "source": fact["source"],
            "bbox": {key: fact[key] for key in ("x0", "y0", "x1", "y1")}}


def _visual_material_row_anchor(fact, table, rows):
    """Require a positioned physical table row, rather than page containment."""
    if fact.get("bbox_clamped") or not _valid_anchor(fact):
        return False
    cells=table["cells"]
    if not _overlap(cells["spec"],fact):
        return False
    top=min(cell["y0"] for cell in cells.values());bottom=max(cell["y1"] for cell in cells.values())
    center=(top+bottom)/2
    others=[]
    other_spans=[]
    for row in rows:
        if row is table or row["unit_id"]!=table["unit_id"]:
            continue
        other_cells=row["cells"]
        y=(min(cell["y0"] for cell in other_cells.values())+max(cell["y1"] for cell in other_cells.values()))/2
        if abs(y-center)<1e-7:
            return False
        others.append(y)
        other_spans.append((min(cell["y0"] for cell in other_cells.values()),max(cell["y1"] for cell in other_cells.values())))
    # In a single-row table, the physical cell height supplies a conservative
    # local limit. Multiple rows additionally supply their actual pitch/bands.
    pitch=min((abs(y-center) for y in others),default=2.5*(bottom-top))
    if pitch<=0 or fact["y1"]-fact["y0"]>1.5*pitch:
        return False
    previous=max((y for y in others if y<center),default=center-pitch)
    following=min((y for y in others if y>center),default=center+pitch)
    band_top,band_bottom=(previous+center)/2,(following+center)/2
    visual_center=(fact["y0"]+fact["y1"])/2
    overlap=lambda low,high:max(0,min(high,fact["y1"])-max(low,fact["y0"]))
    # Visual row boxes may include white space/descent beyond the CAD glyph
    # band. Require substantial support from this row's actual cells while
    # rejecting a box that substantially includes a neighboring physical row.
    return (band_top<=visual_center<=band_bottom and overlap(top,bottom)>=.5*(bottom-top) and
            all(overlap(low,high)<.5*(high-low) for low,high in other_spans))


def run_semantic_comparisons(store, version_id, units, *, task_id=None, persist=False):
    groups = active_records(store, version_id, units, task_id=task_id)
    if not groups:
        return {"comparisons": [], "issues": []}
    _, facts = selected_facts(store, version_id)
    originals = {fact["id"]: fact for fact in store.list_records("facts", version_id=version_id)
                 if fact["source"] == Source.VISION.value}
    visual = [{**fact, "original_visual_text": originals[fact["id"]]["text"]} for fact in facts if fact["id"] in originals]
    def anchor(fact):
        return tuple(fact[key] for key in ("unit_id", "text", "x0", "y0", "x1", "y1"))

    unsure = {anchor(row) for row in store.list_records("pending_confirmations", version_id=version_id)
              if row["status"] == "无法确定"}
    annotations = {unit["id"]: unit["metadata"].get("fact_annotations") or {} for unit in units}
    visual.extend({**fact, **annotations[fact["unit_id"]].get(fact["id"], {}), "manual_unsure": True}
                  for fact in originals.values() if anchor(fact) in unsure)
    comparisons, issues = [], []

    def record_pair(left, right, category, label, left_value, right_value, outcome, basis):
        a, b = _primary(left), _primary(right)
        left_support, left_conflicts = _vision_readings(left, visual)
        right_support, right_conflicts = _vision_readings(right, visual)
        conflicts = left_conflicts + right_conflicts
        if conflicts:
            outcome, basis = "unverified", "同一标注的文字语义与看图读数冲突，需核对原图"
        row = comparison_row(a, b, category=category, label=label, outcome=outcome, basis=basis,
                             left_value=left_value, right_value=right_value)
        proof = {"left": [_audit(fact) for fact in left["cells"].values()],
                 "right": [_audit(fact) for fact in right["cells"].values()],
                 "left_reader": left.get("reader", "model_anchored_text"),
                 "right_reader": right.get("reader", "model_anchored_text"),
                 "vision_support": [_audit(fact) for fact in left_support + right_support],
                 "vision_conflicts": [_audit(fact) for fact in conflicts]}
        row["semantic_evidence"] = proof
        comparisons.append(row)
        if persist and outcome == "mismatch":
            field = "规格" if category == "材料规格" else "数量"
            issues.append(write_issue(store, version_id, title=f"{label}{field}不一致（{left_value} / {right_value}）",
                left=evidence_from_fact(a), right=evidence_from_fact(b), category=category, basis=basis,
                left_value=left_value, right_value=right_value, extra={"match_basis": "anchored_text_semantic",
                    "object_label": label, "semantic_evidence": proof}))

    for (_, family), raw_rows, records in groups:
        by_role = defaultdict(list)
        for item in records:
            by_role[item["role"]].append(item)

        # Model-free specification reading uses a complete model adjacent to an
        # explicit cable use in the design notes, and the same named physical
        # material-table row. The count is deliberately irrelevant to identity.
        table_rows = [row for row in raw_rows if row["role"] == "material_table"]
        note_ids = {row["unit_id"] for row in raw_rows if row["role"] == "design_total"}
        for table in table_rows:
            name = compact(table["cells"]["object"]["text"])
            spec = canonical_cable_spec(table["cells"]["spec"]["text"])
            table_cells = table["cells"]
            table_reader = "local_anchored_text"
            visual_specs = defaultdict(list)
            for fact in facts:
                if (fact["unit_id"] == table["unit_id"] and fact["source"] in {Source.VISION.value, Source.USER.value}
                        and _visual_material_row_anchor(fact,table,table_rows)):
                    read = table_cable_specification(fact["text"], compact(table_cells["serial"]["text"]), name,
                        row_identity=fact.get("object_id") if fact.get("quantity_role")=="material_table" else None)
                    if read:
                        visual_specs[read].append(fact)
            if visual_specs:
                table_cells = {**table_cells, **{f"visual_reading_{index}": fact
                    for index, fact in enumerate((fact for anchors in visual_specs.values() for fact in anchors), 1)}}
            if spec is None and len(visual_specs) == 1:
                spec, anchors = next(iter(visual_specs.items()))
                table_cells = {**table_cells, "visual_spec": anchors[0]}
                table_reader = "saved_visual_anchored_table"
            if not spec or sum(compact(row["cells"]["object"]["text"]) == name for row in table_rows) != 1:
                continue
            candidates = defaultdict(list)
            for fact in facts:
                if fact["unit_id"] not in note_ids or fact["source"] == Source.VISION.value or not _valid_anchor(fact):
                    continue
                for item in cable_specification(fact["text"]):
                    if item["name"] == name:
                        candidates[item["spec"]].append(fact)
            # Multiple conflicting note models need more context; no arbitrary
            # choice of a convenient matching mention is allowed.
            if len(candidates) != 1:
                continue
            note_spec, anchors = next(iter(candidates.items()))
            anchor = max(anchors, key=lambda fact: fact.get("confidence", 1.))
            left = {"unit_id": anchor["unit_id"], "kind": "material_spec", "reader": "local_anchored_text", "cells": {"statement": anchor}}
            right = {"unit_id": table["unit_id"], "kind": "material_spec", "reader": table_reader, "cells": table_cells}
            outcome = "match" if note_spec == spec else "mismatch"
            if len(visual_specs) > 1 or any(value != spec for value in visual_specs):
                outcome = "unverified"
            record_pair(left, right, "材料规格", name, note_spec, spec,
                        outcome,
                        f"同一卷册 {family} 说明中明确用途电缆的完整型号与唯一同名材料表行核对")

        candidate_pairs = [(left, right) for left, right in product(by_role["design_total"], by_role["material_table"])
                           if comparable_quantities(left, right)]
        table_candidates = [{"object": _noun(compact(row["cells"]["object"]["text"])),
                             "spec": compact(row["cells"]["spec"]["text"]).upper()}
                            for row in raw_rows if row["role"] == "material_table"]
        for left, right in candidate_pairs:
            if (sum(a is left for a, _ in candidate_pairs) != 1
                    or sum(b is right for _, b in candidate_pairs) != 1
                    or len(list(quantity_note_candidates(left, raw_rows))) != 1
                    or sum(comparable_quantities(left, candidate) for candidate in table_candidates) != 1):
                continue
            unit_names = {"m": "米"}
            same_unit = unit_names.get(left["unit"], left["unit"]) == unit_names.get(right["unit"], right["unit"])
            outcome = ("match" if amount(left["value"]) == amount(right["value"]) else "mismatch") if same_unit else "unverified"
            basis = f"同一卷册 {family} 的设计总量与唯一材料表行按名称和适用规格核对"
            if not left["spec"]:
                basis = f"同一卷册 {family} 说明泛称与唯一对应材料行核对数量；说明未给规格，未核实规格一致性"
            if not same_unit:
                basis = "说明和材料表的计量单位不同，未核实换算或统计口径"
            label = f'{left["object"]} {left["spec"]}'.strip()
            record_pair(left, right, "数量与材料表", label, f'{left["value"]} {left["unit"]}',
                        f'{right["value"]} {right["unit"]}', outcome, basis)

        by_tag = defaultdict(lambda: {"overview": [], "equipment_scope": []})
        for item in by_role["overview"] + by_role["equipment_scope"]:
            by_tag[item["tag"]][item["role"]].append(item)
        for tag, tagged in by_tag.items():
            overview_rows = [row for row in raw_rows if row["role"] == "overview" and compact(row["cells"]["label"]["text"]) == tag]
            scope_rows = [row for row in raw_rows if row["role"] == "equipment_scope"
                          and tag in asset_tags(row["cells"]["statement"]["text"], row["role"])]
            if len(overview_rows) != 1 or len(scope_rows) != 1 or len(tagged["overview"]) != 1 or len(tagged["equipment_scope"]) != 1:
                continue
            overview, detail = tagged["overview"][0], tagged["equipment_scope"][0]
            record_pair(overview, detail, "设备位号", tag, tag, tag, "match",
                        f"同一卷册 {family} 总图编号和明示设备说明对应详图适用范围")
    return {"comparisons": comparisons, "issues": issues}
