"""Compare local readings when there is a shared object identity and two anchors."""
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from itertools import combinations
import re
from types import SimpleNamespace

from backend.store import BBox, Evidence, Source

from .common import load_tolerances, write_issue
from .dwg_dimensions import run_dwg_dimensions
from .unit_declaration import comparison_declarations, unverified_units_reason, volume_units
from backend.evidence_selection import selected_facts
from backend.extract.engineering import stated_access_paths
from backend.extract.semantic import _FAMILY, _valid_anchor


_ROLE = re.compile(r"标高|净高|高度|长度|宽度|厚度|间距|跨度|直径|埋深|深度")
_TAG = re.compile(r"(?<![A-Za-z0-9])(?:\d?[A-Z]{2,7}-?\d{2,5}|[A-Z]{1,3}-?\d{1,4})(?![A-Za-z0-9])")
_VIEW = re.compile(r"(?:基础|支架|桩基|平台|支墩|电缆沟|检查井|构架|建筑|杆塔)")
_VIEW_SUFFIX = re.compile(r"(?:平面|剖面|立面|断面|布置|详图|配筋|结构|施工|图)+$")
_WEAK = {"", "本图", "相对", "绝对", "建筑", "结构", "设计", "室内", "室外", "地面", "地坪",
         "基础", "顶面", "底面", "基础顶", "基础底", "地面至", "顶", "底"}


def _scope(unit, reading, label, entry):
    if entry.get("building") and entry.get("position_key"):
        return ("roof", unit["original_id"], entry["building"], entry["position_key"])
    tags = sorted(set(_TAG.findall(label)))
    if len(tags) == 1:
        return ("tag", tags[0])
    title = (reading.get("title") or {}).get("name") or {}
    caption = _VIEW_SUFFIX.sub("", re.sub(r"\s+", "", title.get("text") or ""))
    if label == "视频监控室外立杆高度" and "监控" in caption:
        return ("source", unit["original_id"], label)
    if caption and _VIEW.search(caption) and caption not in _WEAK:
        return ("source", unit["original_id"], caption)
    return None


def _numeric(entry):
    raw = entry["value"]
    if raw.startswith("±"):
        if not re.fullmatch(r"±0(?:\.0+)?", raw):
            return None
        raw = raw[1:]
    symbol = raw[0].upper() if raw and raw[0].upper() in "ΦØR" else ""
    if symbol:
        raw = raw[1:]
    try:
        values = tuple(Decimal(part)*(entry["unit_use"].scale or 1) for part in re.split(r"[×xX]", raw))
    except InvalidOperation:
        return None
    if not all(value.is_finite() for value in values):
        return None
    return symbol.replace("Ø", "Φ"), values


def _evidence(unit, item):
    evidence = item["evidence"]
    return Evidence(unit, Source(evidence["source"]), evidence["text"], BBox(**evidence["bbox"]))


def _ambiguous(rows):
    values = {(_numeric(row), row["unit_use"].scale is not None) for row in rows}
    if len(values) != 1 or next(iter(values))[0] is None:
        return None
    return max(rows, key=lambda row: row["evidence"]["confidence"])


def _manual_geometry_conflicts(decisions, unit_id, reading):
    conflicts = []
    for anchor in reading.get("endpoint_evidence") or []:
        tags = set(_TAG.findall(anchor["text"]))
        if not tags:
            continue
        box = anchor["bbox"]
        for decision in decisions:
            if decision["unit_id"] != unit_id or not tags.intersection(_TAG.findall(decision.get("text", ""))):
                continue
            if not all(box[k] < decision[j] and decision[k] < box[j] for k,j in (("x0","x1"),("y0","y1"))):
                continue
            status = decision["status"]
            if status == "无法确定" or (status == "已确认" and not tags.issubset(_TAG.findall(decision.get("confirmed_text") or ""))):
                conflicts.append({key: decision.get(key) for key in ("id","unit_id","status","text","confirmed_text","x0","y0","x1","y1")})
    return conflicts


def _statement_accesses(store, version_id):
    if not hasattr(store,"list_records"):
        return []
    units, facts = selected_facts(store,version_id)
    words = defaultdict(list)
    for fact in facts:
        if fact["source"] == Source.VISION.value or not _valid_anchor(fact):
            continue
        def evidence(fact=fact):
            return {"text":fact["text"], "source":fact["source"], "confidence":fact.get("confidence",1.),
                    "fact_id":fact["id"], "bbox":{key:fact[key] for key in ("x0","y0","x1","y1")}}
        words[fact["unit_id"]].append(SimpleNamespace(text=fact["text"],source=fact["source"],score=fact.get("confidence",1.),
            box=tuple(fact[key] for key in ("x0","y0","x1","y1")), evidence=evidence))
    return [(uid,path) for uid,readings in words.items() for path in stated_access_paths(readings)]


def _candidate_index(units, declarations):
    measurements = defaultdict(lambda: defaultdict(list))
    links = defaultdict(dict)
    signals = defaultdict(lambda: {"source": [], "destination": []})
    assets = defaultdict(lambda: {"equipment_scope": [], "overview": []})
    overview_tags = defaultdict(dict)
    cables = defaultdict(lambda: defaultdict(list))
    connections = defaultdict(lambda: defaultdict(list))
    cable_ranges = defaultdict(lambda: defaultdict(list))
    range_seen = set()
    feeder_routes=[]
    for unit in units:
        meta = unit.get("metadata") or {}
        if meta.get("out_of_scope"):
            continue
        reading = meta.get("local_sheet") or {}
        engineering = reading.get("engineering") or {}
        uid = unit["id"]
        for item in engineering.get("measurements") or []:
            label = item.get("label", "")
            scope = _scope(unit, reading, label, item)
            if not scope or not _ROLE.search(label):
                continue
            use = declarations.for_fact({**item, "unit_id": uid, "text": item["evidence"]["text"]},
                                        item["category"], positioned_value=item["value"])
            item = {**item, "unit_use": use}
            datum = item.get("datum", "")
            if item["category"] == "标高" and not datum:
                continue
            if item["category"] == "标高" and datum in {"相对", "绝对", "当地高程"}:
                scope = (*scope, unit["original_id"])
            identity_label = item["building"] if item.get("position_key") else label
            key = item["category"], scope, identity_label, datum
            measurements[key][uid].append(item)
        for item in engineering.get("links") or []:
            key = item["domain"], tuple(item["endpoints"])
            links[key][uid] = (unit["original_id"], item)
        for item in engineering.get("signals") or []:
            signals[item["code"]][item["direction"]].append((uid, item))
        for item in engineering.get("assets") or []:
            view = item.get("view")
            if view in {"equipment_scope", "overview"}:
                assets[item["tag"]][view].append((uid, item))
            if view == "overview":
                overview_tags[unit["original_id"], uid][item["tag"]] = item
        for item in engineering.get("cables") or []:
            if item.get("kind")=="cable_range":
                marker=(uid,item["view"],item["code"],item["cabinet"])
                if marker not in range_seen:
                    cable_ranges[unit["original_id"],item["cabinet"]][item["view"]].append((uid,item))
                    range_seen.add(marker)
            else:
                cables[unit["original_id"], item["code"]][item["view"]].append((uid, item))
        # Native records are the parser-owned source. A local OCR cache can be
        # older even when its image hash is unchanged after metadata-only repair.
        native_connections = meta.get("native_connections") if "native_connections" in meta else engineering.get("connections")
        for item in native_connections or []:
            if item.get("kind")=="feeder_reachability":
                if item.get("relation")=="reachable_from_cabinet" and item.get("geometry",{}).get("handles"):
                    feeder_routes.append((uid,item))
            elif item.get("kind")=="cable_range":
                marker=(uid,item["view"],item["code"],item["cabinet"])
                if marker not in range_seen:
                    cable_ranges[unit["original_id"],item["cabinet"]][item["view"]].append((uid,item))
                    range_seen.add(marker)
            elif item.get("view") in {"schedule", "schematic"} and item.get("geometry", {}).get("handles"):
                connections[unit["original_id"], item["circuit_code"]][item["view"]].append((uid,item))
    return measurements, links, signals, assets, overview_tags, cables, connections, cable_ranges, feeder_routes


def run_engineering(store, version_id, *, task_id=None, settings=None, persist=True, units=None, declarations=None):
    tolerances = load_tolerances(store, task_id, settings)
    if units is None:
        units = store.list_records("drawing_units", version_id=version_id)
    units = list(units)
    if declarations is None:
        declarations = volume_units(store, version_id, units=units)
    measured, links, signals, assets, overview_tags, cables, connections, cable_ranges, feeder_routes = _candidate_index(units, declarations)
    decisions = store.list_records("pending_confirmations", version_id=version_id) if hasattr(store,"list_records") else []
    shared = run_dwg_dimensions(store, version_id, units=units, task_id=task_id, settings=settings,
                                persist=persist, declarations=declarations)
    comparisons, issues = shared["comparisons"], shared["issues"]
    for (category, _, label, datum), by_unit in measured.items():
        candidates = [(uid, entry) for uid, rows in sorted(by_unit.items())
                      if (entry := _ambiguous(rows)) is not None]
        for (left_id,left), (right_id,right) in combinations(candidates, 2):
            display_label = f'{label} {left["label"]}' if left.get("position_key") else label
            left_symbol, lhs = _numeric(left)
            right_symbol, rhs = _numeric(right)
            if left_symbol != right_symbol or len(lhs) != len(rhs):
                continue
            elevation = category == "标高"
            key = "elevation_tolerance_m" if elevation else "dimension_tolerance_mm"
            tolerance = Decimal(str(tolerances[key]))
            a, b = left["unit_use"], right["unit_use"]
            unit_evidence = comparison_declarations(a, b)
            diff = None
            if a.scale is None or b.scale is None:
                outcome, basis = "unverified", unverified_units_reason(category, a, b)
            else:
                diff = max(abs(x-y) for x,y in zip(lhs,rhs)) / (1000 if elevation else 1)
                outcome = "mismatch" if diff > tolerance else "match"
                basis = f"相差 {diff} {'m' if elevation else 'mm'}，容差 {tolerance} {'m' if elevation else 'mm'}"
            left_display = f'{left["value"]} {left["unit"]}'.strip()
            right_display = f'{right["value"]} {right["unit"]}'.strip()
            comparisons.append({"category": category, "label": display_label, "datum": datum, "outcome": outcome,
                                "left_unit_id": left_id, "right_unit_id": right_id,
                                "left_value": left_display, "right_value": right_display,
                                "displayed_only": diff is None, "basis": basis, "unit_declarations": unit_evidence,
                                "left_evidence": left["evidence"], "right_evidence": right["evidence"]})
            if outcome == "mismatch":
                title = f"{display_label}不一致（{left_display} / {right_display}）"
                basis = f"同一对象和标注位置，差值 {diff} {'m' if elevation else 'mm'}，超过容差"
                kwargs = dict(title=title, category=category, left_value=left_display, right_value=right_display, basis=basis)
                if persist:
                    issues.append(write_issue(store, version_id, left=_evidence(left_id,left), right=_evidence(right_id,right),
                                              extra={"match_basis": "local_engineering_identity", "object_label": display_label,
                                                     "tolerance_m" if elevation else "tolerance_mm": float(tolerance),
                                                     "abs_diff" if elevation else "max_abs_diff": str(diff),
                                                     "unit_declarations": unit_evidence, "datum": datum}, **kwargs))
                else:
                    issues.append(kwargs)
    for (domain, endpoints), by_unit in links.items():
        for (left_id, (left_source,left)), (right_id, (right_source,right)) in combinations(sorted(by_unit.items()), 2):
            if left_source != right_source and domain != "光纤":
                continue
            if left["view"] == right["view"] and left["view"] != "statement":
                continue
            manual_conflicts = (_manual_geometry_conflicts(decisions,left_id,left) +
                                _manual_geometry_conflicts(decisions,right_id,right))
            comparisons.append({"category": "接线", "label": " ↔ ".join(endpoints), "domain": domain,
                                "outcome": "unverified" if manual_conflicts else "match", "left_unit_id": left_id, "right_unit_id": right_id,
                                "left_evidence": left["evidence"], "right_evidence": right["evidence"],
                                "basis": ("对应设备位号存在人工改值或无法确定记录，未用局部几何读数覆盖人工判断" if manual_conflicts else
                                          "总图与详图均有设备间连续路径，未穿过第三设备；未核实光纤芯号或端口" if domain == "光纤" else "两侧明确端点关系一致"),
                                "manual_conflicts": manual_conflicts,
                                "local_geometry": {"left": left.get("geometry"), "right": right.get("geometry")},
                                "endpoint_evidence": {"left": left.get("endpoint_evidence", []), "right": right.get("endpoint_evidence", [])}})
        if domain != "光纤":
            continue
        for detail_id, (source, item) in by_unit.items():
            if item["view"] != "detail":
                continue
            for (overview_source, overview_id), tags in overview_tags.items():
                if source != overview_source or overview_id in by_unit or not all(tag in tags for tag in endpoints):
                    continue
                comparisons.append({"category": "接线", "label": " ↔ ".join(endpoints), "domain": domain,
                                    "outcome": "unverified", "basis": "详图已读出线路，网络总图未能确认对应路径", "left_unit_id": detail_id, "right_unit_id": overview_id,
                                    "left_evidence": item["evidence"], "right_evidence": tags[endpoints[0]]["evidence"]})
    for code, ends in signals.items():
        if len(ends["source"]) != 1:
            continue
        left_id, left = ends["source"][0]
        for right_id, right in ends["destination"]:
            if left_id == right_id:
                continue
            wires = left["wire"], right["wire"]
            outcome = "mismatch" if all(wires) and wires[0] != wires[1] else "match" if all(wires) else "linked"
            comparisons.append({"category": "接线", "label": code, "domain": "电气", "outcome": outcome,
                                "left_unit_id": left_id, "right_unit_id": right_id,
                                "left_evidence": left["evidence"], "right_evidence": right["evidence"]})
            if outcome == "mismatch":
                kwargs = dict(title=f"{code}跨图线号不一致", category="接线", left_value=wires[0], right_value=wires[1],
                              basis="唯一来源与目的信号箭头具有相同信号码，图示线号不同")
                if persist:
                    issues.append(write_issue(store, version_id, left=_evidence(left_id,left), right=_evidence(right_id,right),
                                              extra={"match_basis": "cad_signal_code", "object_label": code}, **kwargs))
                else:
                    issues.append(kwargs)
    for tag, by_view in assets.items():
        scope, overview = by_view["equipment_scope"], by_view["overview"]
        if len(scope) != 1 or len(overview) != 1 or scope[0][0] == overview[0][0]:
            continue
        (left_id,left), (right_id,right) = scope[0], overview[0]
        comparisons.append({"category": "设备位号", "label": tag, "outcome": "match",
                            "left_unit_id": left_id, "right_unit_id": right_id,
                            "left_evidence": left["evidence"], "right_evidence": right["evidence"]})
    for (_, code), by_view in cables.items():
        schematics, schedules = by_view["schematic"], by_view["schedule"]
        if len(schematics) != 1 or len(schedules) != 1:
            continue
        (left_id,left), (right_id,right) = schematics[0], schedules[0]
        if left_id != right_id:
            comparisons.append({"category": "接线", "label": f"电缆编号 {code}", "outcome": "match",
                                "domain": "电缆编号", "cabinet": right["cabinet"],
                                "basis": f'一次系统图与配电装置图的电缆编号一致，表内对应 {right["cabinet"]} 柜列',
                                "left_unit_id": left_id, "right_unit_id": right_id,
                                "left_evidence": left["evidence"], "right_evidence": right["evidence"]})
    for (_,cabinet),by_view in cable_ranges.items():
        if len(by_view["schematic"])!=1 or len(by_view["schedule"])!=1:
            continue
        (left_id,left),(right_id,right)=by_view["schematic"][0],by_view["schedule"][0]
        if left_id==right_id or not all(item.get("geometry",{}).get("handles") for item in (left,right)):
            continue
        range_units={unit["id"]:unit for unit in units}
        left_family=_FAMILY.fullmatch(range_units[left_id].get("drawing_number", ""))
        right_family=_FAMILY.fullmatch(range_units[right_id].get("drawing_number", ""))
        if not left_family or not right_family or left_family[1]!=right_family[1]:
            continue
        conflicts=_manual_geometry_conflicts(decisions,left_id,{"endpoint_evidence":[left["evidence"],*left["endpoint_evidence"]]})+_manual_geometry_conflicts(decisions,right_id,{"endpoint_evidence":[right["evidence"],*right["endpoint_evidence"]]})
        same=left["range_key"]==right["range_key"]
        outcome="unverified" if conflicts else "match" if same else "mismatch"
        basis=("同柜电缆编号字段的范围原文字面不同，未判断哪侧正确；未枚举范围或自动纠号" if not same else "同柜电缆编号范围仅书写方式不同，起止标识等价")
        if conflicts:
            basis="电缆编号或柜号存在人工改值或无法确定记录，未覆盖人工判断"
        row={"category":"接线","label":f"{cabinet} 电缆编号范围","domain":"电缆编号范围","relation":"cable_range_literal",
             "outcome":outcome,"left_unit_id":left_id,"right_unit_id":right_id,"basis":basis,
             "left_value":left["evidence"]["text"],"right_value":right["evidence"]["text"],
             "left_evidence":left["evidence"],"right_evidence":right["evidence"],
             "endpoint_evidence":{"left":left["endpoint_evidence"],"right":right["endpoint_evidence"]},
             "local_geometry":{"left":left["geometry"],"right":right["geometry"]},"manual_conflicts":conflicts}
        comparisons.append(row)
        if outcome=="mismatch":
            kwargs=dict(title=f"{cabinet}电缆编号范围疑似不一致",category="接线",basis=basis,
                        left_value=row["left_value"],right_value=row["right_value"])
            if persist:
                issues.append(write_issue(store,version_id,left=_evidence(left_id,left),right=_evidence(right_id,right),
                    extra={"match_basis":"cad_cable_range_field","object_label":cabinet,
                           "local_geometry":row["local_geometry"],"endpoint_evidence":row["endpoint_evidence"]},**kwargs))
            else:
                issues.append(kwargs)
    connection_coverage = []
    for (_, code), by_view in connections.items():
        schematics, schedules = by_view["schematic"], by_view["schedule"]
        for view, records in by_view.items():
            for uid, record in records:
                connection_coverage.append({"unit_id": uid, "circuit_code": code, "view": view,
                    "status": "paired" if len(schematics) == len(schedules) == 1 else "one_sided_or_ambiguous",
                    "reason": "已读取引线、出线箭头和柜列；另侧尚无可独立核实的线路端点几何" if not schematics else "",
                    "record": record})
        if len(schematics) != 1 or len(schedules) != 1:
            continue
        (left_id,left), (right_id,right) = schematics[0], schedules[0]
        if left_id == right_id:
            continue
        same = left["cabinet"] == right["cabinet"] and set(left["endpoint_tags"]) == set(right["endpoint_tags"])
        complete = left.get("endpoint_set_complete") and right.get("endpoint_set_complete")
        outcome = "match" if same else "mismatch" if complete else "unverified"
        basis = ("同一线路编号，两侧独立几何均绑定相同柜号及明示远端设备"
                 if same else "同一线路编号的柜号或远端设备不同，尚需核实两侧是否列全端点")
        comparisons.append({"category": "接线", "label": f"{code} 柜列与远端设备", "domain": "电气线路端点",
                            "outcome": outcome, "left_unit_id": left_id, "right_unit_id": right_id,
                            "left_value": left["cabinet"]+" → "+"、".join(left["endpoint_tags"]),
                            "right_value": right["cabinet"]+" → "+"、".join(right["endpoint_tags"]),
                            "left_evidence": left["evidence"], "right_evidence": right["evidence"], "basis": basis,
                            "local_geometry": {"left": left["geometry"], "right": right["geometry"]},
                            "endpoint_evidence": {"left": left["endpoint_evidence"], "right": right["endpoint_evidence"]}})
        if outcome == "mismatch":
            row = comparisons[-1]
            kwargs = dict(title=f"{code}柜列或远端设备不一致", category="接线", basis="两侧均明确列全线路端点，柜列或端点集合不同",
                          left_value=row["left_value"], right_value=row["right_value"])
            if persist:
                issues.append(write_issue(store,version_id,left=_evidence(left_id,left),right=_evidence(right_id,right),
                    extra={"match_basis":"cad_circuit_endpoint_geometry", "object_label":code,
                           "local_geometry":row["local_geometry"], "endpoint_evidence":row["endpoint_evidence"]}, **kwargs))
            else:
                issues.append(kwargs)
    # Compare the weaker shared predicate: a named transformer is reachable
    # from this cabinet. A complete affirmative text chain is independent of
    # the other sheet's leader/arrow/column binding; neither asserts adjacency.
    statement_index = defaultdict(list)
    schedule_index = defaultdict(list)
    units_by_id = {unit["id"]:unit for unit in units}

    def family_key(uid):
        unit = units_by_id.get(uid)
        if not unit or unit.get("metadata",{}).get("title_identity_uncertain"):
            return None
        family = _FAMILY.fullmatch(unit.get("drawing_number", ""))
        return (unit["original_id"],family[1]) if family else None

    for uid,path in _statement_accesses(store,version_id):
        scope=family_key(uid)
        if scope:
            statement_index[scope,path["cabinet"],path["source_tag"]].append((uid,path))
    for uid,path in feeder_routes:
        scope=family_key(uid)
        if scope:
            statement_index[scope,path["cabinet"],path["source_tag"]].append((uid,path))
    for by_view in connections.values():
        for uid,schedule in by_view["schedule"]:
            scope=family_key(uid)
            if scope:
                for tag in schedule["endpoint_tags"]:
                    schedule_index[scope,schedule["cabinet"],tag].append((uid,schedule))
    reachable_coverage = []
    matched = defaultdict(set)
    for key,schedules in schedule_index.items():
        _,cabinet,tag=key
        statements=statement_index[key]
        # Independent positive text and drawn-component readings on the same
        # sheet support one predicate. They are not duplicate business pairs or
        # two competing candidate sheets.
        by_source_unit=defaultdict(list)
        for uid,statement in statements:
            if statement not in by_source_unit[uid]:
                by_source_unit[uid].append(statement)
        if len(by_source_unit) != 1 or len(schedules) != 1 or next(iter(by_source_unit),None) == schedules[0][0]:
            reachable_coverage.append({"relation":"reachable_from_cabinet", "cabinet":cabinet,"source_tag":tag,
                "outcome":"unverified", "reason":"已有独立柜列引线，另一图尚无唯一肯定原文路径或已识别符号路径"})
            continue
        left_id,supports=next(iter(by_source_unit.items()));right_id,right=schedules[0]
        left=next((support for support in supports if support.get("path")),supports[0])
        geometry_support=next((support for support in supports if support.get("kind")=="feeder_reachability"),None)
        conflicts=[conflict for support in supports for conflict in _manual_geometry_conflicts(decisions,left_id,support)]
        conflicts+=_manual_geometry_conflicts(decisions,right_id,right)
        conflicts=list({conflict.get("id"):conflict for conflict in conflicts}.values())
        outcome="unverified" if conflicts else "match"
        note=next((item for item in right["endpoint_evidence"] if tag in _TAG.findall(item["text"])),right["evidence"])
        left_anchors=[]
        for support in supports:
            for anchor in support["endpoint_evidence"]:
                if anchor not in left_anchors:left_anchors.append(anchor)
        left_value=" → ".join(left["path"]) if left.get("path") else f"{cabinet} 馈线接入 {tag}"
        comparisons.append({"category":"接线","label":f"{tag} 可达接入 {cabinet}","domain":"电气可达接入",
            "relation":"reachable_from_cabinet", "outcome":outcome, "left_unit_id":left_id,"right_unit_id":right_id,
            "left_value":left_value, "right_value":f"{tag} 接入 {cabinet}",
            "left_evidence":left["endpoint_evidence"][0], "right_evidence":note,
            "basis":("对应原文存在人工改值或无法确定记录，未覆盖人工判断" if conflicts else
                     "总图的肯定说明或独立柜框、真实导线与已识别组件端口支持该设备归属馈线，装置图独立引线与柜列支持同一关系；不判断图示开关当前导通、单根电缆直接端点或完整终点清单"),
            "endpoint_evidence":{"left":left_anchors,"right":right["endpoint_evidence"]},
            "statement_path":left.get("path",[]), "local_geometry":{"left":geometry_support.get("geometry") if geometry_support else None,"right":right["geometry"]},
            "supporting_sources":supports,
            "wire_segment_markers":geometry_support.get("wire_segment_markers",[]) if geometry_support else [],
            "manual_conflicts":conflicts})
        reachable_coverage.append({"relation":"reachable_from_cabinet","cabinet":cabinet,"source_tag":tag,"outcome":outcome})
        if outcome=="match":
            matched[right_id,right["circuit_code"]].add(tag)
    for item in connection_coverage:
        verified=matched[item["unit_id"],item["circuit_code"]]
        if verified:
            item["matched_endpoints"]=sorted(verified)
            item["unverified_endpoints"]=sorted(set(item["record"]["endpoint_tags"])-verified)
            item["status"]="partially_paired" if item["unverified_endpoints"] else "paired"
            item["reason"]="仅明示的单成员可达关系已对照，其余端点仍缺另一侧独立路径" if item["unverified_endpoints"] else ""
    from .semantic import run_semantic_comparisons
    semantic = run_semantic_comparisons(store, version_id, units, task_id=task_id, persist=persist)
    return {"issues": issues + semantic["issues"], "comparisons": comparisons + semantic["comparisons"],
            "connection_coverage": connection_coverage,"reachable_access_coverage":reachable_coverage}
