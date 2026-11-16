"""Evidence-preserving, opt-in layout experiment. No provider or storage dependency."""
from __future__ import annotations

from copy import deepcopy
import json
from math import ceil, floor, isfinite
import re


VERSION = "layout-context-v1"
REGION_TYPES = ("notes", "table", "dimension_chain", "title", "other")
BOUNDARY_STATES = ("complete", "partial", "unknown")
BOX_KEYS = ("x0", "y0", "x1", "y1")
SCALAR = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?\Z")
COMPARATORS = frozenset({"<", ">", "=", "<=", ">=", "≤", "≥", "≦", "≧", "≠", "≈", "±", "∓",
                         "~", "～", "＜", "＞", "＝", "＜＝", "＞＝"})
QUALIFIERS = frozenset("".join(COMPARATORS))

LOCATOR_SYSTEM_PROMPT = """你只在原审查图上定位版面。只输出一个 JSON 对象。所有框均为当前原图左上原点、0~1归一化坐标。按完整说明段、完整材料表、完整尺寸链、图签、其他区域定位。尺寸链框须连同方向、首尾尺寸界线、附近构件或引线留在同一上下文；表区保留表头和全部可见行；说明保留整段和相邻续行。不把图名、目录表头、资质号当图签身份。无法辨清边界或存在未定位区域时明确说明，不猜图号、事实或完整覆盖。"""
LOCATOR_USER_PROMPT = """定位本页的版面区域，按此结构输出：
{"regions":[{"region_type":"notes","bbox":{"x0":0.1,"y0":0.1,"x1":0.8,"y1":0.5},"context_bbox":{"x0":0.08,"y0":0.08,"x1":0.9,"y1":0.6},"boundary_status":"complete"}],"unlocated":[{"region_type":"other","reason":"未能定位的图面内容"}]}
region_type为notes/table/dimension_chain/title/other。bbox框住该结构，context_bbox必须包含bbox及支撑关系的周边证据。boundary_status为complete/partial/unknown，仅描述观察到的边界。示例坐标只说明格式，按原图独立定位。"""
REGION_SYSTEM_PROMPT = """你逐字读取一张原图的版面裁剪。只输出一个JSON对象。坐标以本次输入裁剪为基准，左上原点、0~1归一化。完整保留可见说明行、续行、表头、物理表行、尺寸链和相关引注；保留重复物理行。读不清填unknown或null，不纠字，不推断单位或对象。模型证据只是候选。"""
ANCHORED_NOTES_SYSTEM_PROMPT = """你按固定行候选逐字读取说明裁剪，只输出一个JSON对象。行位置由程序提供。按输入行ID与顺序返回所有行，输出中不生成任何bbox，不自行定位单字。按可见编号段与续行保留说明语境。"""


def _region_prompt_example(region_type):
    def box(x0, y0, x1, y1):
        return {"x0": x0, "y0": y0, "x1": x1, "y1": y1}

    def line(line_id, text, bbox):
        return {"id": line_id, "text": text, "bbox": bbox}

    def quote(line_id, text):
        return {"line_id": line_id, "quote": text}

    sample_line = line("l1", "试件甲宽度≥12 cm", box(.1, .2, .8, .3))
    sample_line["paragraph_id"] = "p1"
    measurement = {"id": "m1", "literal": quote("l1", "试件甲宽度≥12 cm"),
                   "value": quote("l1", "12"), "unit": quote("l1", "cm"),
                   "comparator": quote("l1", "≥"), "property": quote("l1", "宽度"),
                   "object": quote("l1", "试件甲"), "unit_origin": "explicit",
                   "relation_basis": "same_phrase", "unit_relation": None}
    example = {"context_status": "complete", "lines": [sample_line], "measurements": [measurement],
               "table": None, "chain": None}
    if region_type == "title":
        example["lines"] = [line("l1", "图号 SAMPLE-A", box(.1, .2, .8, .3))]
        example["measurements"] = []
    elif region_type == "table":
        example["lines"] = [line("h1", "材料", box(.12, .12, .38, .19)),
                            line("h2", "数量（件）", box(.52, .12, .78, .19)),
                            line("l1", "样品甲", box(.12, .25, .38, .29)),
                            line("l2", "12", box(.52, .25, .78, .29))]
        example["table"] = {
            "columns": [{"id": "c1", "bbox": box(.1, .1, .4, .4), "header": quote("h1", "材料")},
                        {"id": "c2", "bbox": box(.5, .1, .8, .4), "header": quote("h2", "数量（件）")}],
            "rows": [{"id": "r1", "row_index": 0, "bbox": box(.1, .22, .8, .32),
                      "cells": [{"column_id": "c1", "state": "read", "content": quote("l1", "样品甲")},
                                {"column_id": "c2", "state": "read", "content": quote("l2", "12")}]}],
        }
        measurement.update(literal=quote("l2", "12"), value=quote("l2", "12"), unit=quote("h2", "件"),
                           comparator=None, property=quote("h2", "数量"), object=quote("l1", "样品甲"),
                           unit_origin="inherited", unit_relation=quote("h2", "数量（件）"),
                           relation_basis="table_row", row_id="r1", column_id="c2")
    elif region_type == "dimension_chain":
        example["lines"] = [line("l1", "12", box(.42, .42, .48, .46)),
                            line("l2", "试件甲宽度", box(.12, .14, .32, .19))]
        example["chain"] = {"direction": "horizontal", "endpoints": [{"x": .2, "y": .5}, {"x": .8, "y": .5}],
                            "association_evidence": [quote("l2", "试件甲宽度")],
                            "segments": [{"id": "s1", "bbox": box(.3, .38, .62, .53), "label": quote("l1", "12")}]}
        measurement.update(literal=quote("l1", "12"), value=quote("l1", "12"), unit=None, comparator=None,
                           property=quote("l2", "宽度"), object=quote("l2", "试件甲"), unit_origin="unknown",
                           relation_basis="chain_segment", segment_id="s1")
    return example


def build_region_prompt(region_type):
    _enum(region_type, REGION_TYPES, "region_type")
    example = json.dumps(_region_prompt_example(region_type), ensure_ascii=False, indent=2)
    prompt = f"""当前区域类型为{region_type}。下方是合成示例，展示输出JSON结构。按当前输入图逐字读取，使用实际的内容、行ID和坐标。
{example}

literal、value、unit、comparator、property、object、unit_relation填写示例中的引用对象或null。literal必须为引用对象。line_id指向lines的唯一id，quote逐字引用同一行的连续原文。每个数值的literal保留能区分重复数字的上下文，value只引用完整单个数值。用更长的原文区分重复词，无法确定唯一来源时将相应字段填null并标为unresolved。尺寸规格组合保持原文，不能猜成单个值。
逐行给出实际可见文字框，坐标以输入裁剪为基准。字段引用可带对应文字的局部bbox，位置不确定就省略字段bbox。仅同一可见编号段及其续行共享paragraph_id，不同编号段分别编号，标题或无法分组的行可省略。对象与属性只能来自可定位原文。relation_basis为same_phrase、table_row、chain_segment或unresolved。
unit_origin为explicit、inherited或unknown。继承单位用unit_relation引用实际单位声明或同列表头，unit引用其中单位原文。找不到声明就将单位标记为unknown并填null。没有可见比较符时comparator填null。context_status为complete、partial或unknown，反映当前裁剪中的可见内容与边界。"""
    if region_type == "table":
        prompt += """
表格列按左至右、行按上至下。每物理行逐列提供单元格，保留内容相同的不同物理行。cell的state为read、blank或unknown。每格文字可单独为一行。table_row测量给row_id和column_id，显式单位在同一物理行的另一列时给unit_column_id。继承只能来自该测量列实际可见的表头声明。"""
    elif region_type == "dimension_chain":
        prompt += """
尺寸链direction为horizontal、vertical、oblique或unknown。水平段从左到右、竖向从上到下、斜向沿端点顺序。看不到的端点填null，读不到的段标签填null。chain_segment测量给segment_id。对象和属性须在可关联到尺寸链的实际引注里。"""
    return prompt


def build_anchored_notes_prompt(bands):
    """Give the model fixed ink-band IDs and an output example without geometry."""
    bands = _object(bands, "bands")
    rows = _array(bands.get("lines"), "bands.lines")
    _require(bool(rows), "NO_FIXED_LINES", "bands.lines")
    hints, seen = [], set()
    previous_bottom = 0
    for index, row in enumerate(rows):
        path = f"bands.lines[{index}]"
        row = _object(row, path)
        line_id = _text(row.get("id"), path+".id")
        _require(line_id not in seen, "DUPLICATE_ID", path+".id")
        bbox = _bbox(row.get("bbox"), path+".bbox")
        _require(bbox["y0"] >= previous_bottom, "FIXED_LINE_ORDER", path+".bbox")
        hints.append({"id": line_id, "bbox": bbox})
        seen.add(line_id)
        previous_bottom = bbox["y1"]

    example = _region_prompt_example("notes")
    example["lines"][0].pop("bbox")
    example["lines"][0]["id"] = "demo-line"
    for field in example["measurements"][0].values():
        if isinstance(field, dict):
            field["line_id"] = "demo-line"
    return f"""使用下方合成示例的JSON结构。示例字面和demo-line仅展示格式。
{json.dumps(example, ensure_ascii=False, indent=2)}

按输入裁剪上的固定行候选逐行抄录。输出lines必须按同一顺序列出下方全部实际ID，每个ID仅出现一次。行字段为id、text和可选的paragraph_id。无法读清的行保留ID，text填「未读清」，context_status填partial或unknown。不要将未读清的文字作为测量引用。
固定位置只用于查找原文字。输出lines和所有字段引用都不得带bbox。分别为每个可见编号段分配paragraph_id，仅与该段连续的续行共享ID。标题或无法确认所属段的行可省略paragraph_id。
literal必须是包含line_id及逐字quote的对象。value、unit、comparator、property、object和unit_relation也使用该引用结构或null。使用长literal区分重复数字，value仅引用该literal内完整的单个数值。不能确定属性或对象就保留null，relation_basis填unresolved。继承单位只引用实际声明，缺声明时unit_origin填unknown、unit填null。保留可见的比较符、原文字面和段落关系。table和chain均填null。

固定行候选在裁剪中按从上至下排列。以下bbox为输入提示，采用左上原点0到1归一化坐标。
{json.dumps(hints, ensure_ascii=False, indent=2)}"""


class LayoutContextError(ValueError):
    def __init__(self, code, location):
        self.code = code
        self.location = location
        super().__init__(f"{code}: {location}")


def _require(condition, code, location):
    if not condition:
        raise LayoutContextError(code, location)


def _object(value, location):
    _require(isinstance(value, dict), "EXPECTED_OBJECT", location)
    return value


def _array(value, location):
    _require(isinstance(value, list), "EXPECTED_ARRAY", location)
    return value


def _text(value, location):
    _require(isinstance(value, str) and bool(value.strip()), "EXPECTED_TEXT", location)
    return value


def _enum(value, choices, location):
    _require(value in choices, "INVALID_ENUM", location)
    return value


def _coordinate(value):
    return type(value) in (int, float) and 0 <= value <= 1 and isfinite(value)


def _bbox(value, location):
    box = _object(value, location)
    _require(all(_coordinate(box.get(key)) for key in BOX_KEYS), "INVALID_BBOX", location)
    _require(box["x0"] < box["x1"] and box["y0"] < box["y1"], "INVALID_BBOX", location)
    return {key: box[key] for key in BOX_KEYS}


def _contains(outer, inner):
    return all(outer[axis+"0"] <= inner[axis+"0"] <= inner[axis+"1"] <= outer[axis+"1"] for axis in ("x", "y"))


def _size(size):
    _require(isinstance(size, (list, tuple)) and len(size) == 2 and
             all(type(value) is int and value > 0 for value in size), "INVALID_IMAGE_SIZE", "image_size")
    return size


def plan_regions(payload, image_size, *, max_regions=None):
    """Validate source-page geometry and freeze a native crop schedule."""
    width, height = _size(image_size)
    _require(max_regions is None or (type(max_regions) is int and max_regions >= 0),
             "INVALID_REGION_LIMIT", "max_regions")
    raw = _object(payload, "locator")
    by_type = {kind: [] for kind in REGION_TYPES}
    for index, item in enumerate(_array(raw.get("regions"), "locator.regions")):
        path = f"locator.regions[{index}]"
        item = _object(item, path)
        kind = _enum(item.get("region_type"), REGION_TYPES, path+".region_type")
        box = _bbox(item.get("bbox"), path+".bbox")
        context = _bbox(item.get("context_bbox"), path+".context_bbox")
        _require(_contains(context, box), "CONTEXT_EXCLUDES_REGION", path)
        pixels = [floor(context["x0"]*width), floor(context["y0"]*height),
                  ceil(context["x1"]*width), ceil(context["y1"]*height)]
        by_type[kind].append({"region_type": kind, "bbox": box, "context_bbox": context, "pixel_box": pixels,
                             "boundary_status": _enum(item.get("boundary_status"), BOUNDARY_STATES, path+".boundary_status")})
    for rows in by_type.values():
        rows.sort(key=lambda row: tuple(row["bbox"][key] for key in ("y0", "x0", "y1", "x1")))
    # First take one of each semantic type, then the next occurrence. A second
    # note block cannot consume the first chance to inspect a table or chain.
    regions = [rows[index] for index in range(max(map(len, by_type.values())))
               for rows in by_type.values() if index < len(rows)]
    for index, region in enumerate(regions):
        region.update(id=f"region-{index+1}", state="planned" if max_regions is None or index < max_regions else "not_scheduled")
    unlocated = []
    for index, item in enumerate(_array(raw.get("unlocated", []), "locator.unlocated")):
        path = f"locator.unlocated[{index}]"
        item = _object(item, path)
        unlocated.append({"region_type": _enum(item.get("region_type"), REGION_TYPES, path+".region_type"),
                          "reason": _text(item.get("reason"), path+".reason"), "state": "unknown"})
    return {"version": VERSION, "image_size": [width, height], "regions": regions,
            "unlocated": unlocated, "coverage": "unknown", "whole_complete": False}


class _RegionEvidence:
    def __init__(self, region, image_size, payload):
        self.region = region
        self.width, self.height = _size(image_size)
        pixels = region.get("pixel_box")
        _require(isinstance(pixels, (list, tuple)) and len(pixels) == 4 and
                 all(type(value) is int for value in pixels), "INVALID_PIXEL_BOX", "region.pixel_box")
        left, top, right, bottom = pixels
        _require(0 <= left < right <= self.width and 0 <= top < bottom <= self.height,
                 "INVALID_PIXEL_BOX", "region.pixel_box")
        self.pixels = pixels
        self.lines = {}
        for index, line in enumerate(_array(payload.get("lines"), "lines")):
            path = f"lines[{index}]"
            line = _object(line, path)
            line_id = _text(line.get("id"), path+".id")
            _require(line_id not in self.lines, "DUPLICATE_ID", path+".id")
            paragraph = line.get("paragraph_id")
            if paragraph is not None:
                _text(paragraph, path+".paragraph_id")
            box = _bbox(line.get("bbox"), path+".bbox")
            self.lines[line_id] = {"id": line_id, "text": _text(line.get("text"), path+".text"),
                                   "paragraph_id": paragraph, "local_bbox": box, "bbox": self.map_box(box)}

    def map_point(self, point):
        left, top, right, bottom = self.pixels
        return {"x": (left+point["x"]*(right-left))/self.width,
                "y": (top+point["y"]*(bottom-top))/self.height}

    def map_box(self, box):
        first = self.map_point({"x": box["x0"], "y": box["y0"]})
        last = self.map_point({"x": box["x1"], "y": box["y1"]})
        return {"x0": first["x"], "y0": first["y"], "x1": last["x"], "y1": last["y"]}

    def span(self, raw, path, *, within=None, preferred=None):
        if raw is None:
            return None
        raw = _object(raw, path)
        line_id = _text(raw.get("line_id"), path+".line_id")
        _require(line_id in self.lines, "UNKNOWN_LINE", path)
        line = self.lines[line_id]
        quote = _text(raw.get("quote", raw.get("text")), path+".quote")
        _require("text" not in raw or raw["text"] == quote, "INCONSISTENT_QUOTE", path)
        if (within is None and preferred is not None and raw.get("start") is None and
                line_id == preferred["line_id"] and quote in preferred["quote"]):
            within = preferred
        if within is not None:
            _require(line_id == within["line_id"], "SPAN_OUTSIDE_ANCHOR", path)
        first = within["start"] if within else 0
        last = within["end"] if within else len(line["text"])
        start = raw.get("start")
        if start is None:
            start = line["text"].find(quote, first, last)
            _require(start >= 0, "QUOTE_NOT_FOUND", path)
            _require(line["text"].find(quote, start+1, last) == -1, "AMBIGUOUS_QUOTE", path)
        _require(type(start) is int and first <= start and start+len(quote) <= last and
                 line["text"][start:start+len(quote)] == quote, "INVALID_SPAN_OFFSET", path)
        local = _bbox(raw["bbox"], path+".bbox") if "bbox" in raw else line["local_bbox"]
        _require(_contains(line["local_bbox"], local), "SPAN_OUTSIDE_LINE", path)
        if within and "bbox" in raw and within["bbox_precision"] == "span":
            _require(_contains(within["local_bbox"], local), "SPAN_OUTSIDE_ANCHOR", path)
        return {"line_id": line_id, "quote": quote, "start": start, "end": start+len(quote),
                "local_bbox": dict(local), "bbox": self.map_box(local),
                "bbox_precision": "span" if "bbox" in raw else "line"}

    def same_paragraph(self, spans):
        lines = {span["line_id"] for span in spans if span is not None}
        groups = {self.lines[line]["paragraph_id"] for line in lines}
        return len(lines) <= 1 or (len(groups) == 1 and None not in groups)


def _in_span(span, anchor):
    return bool(span and anchor and span["line_id"] == anchor["line_id"] and
                anchor["start"] <= span["start"] < span["end"] <= anchor["end"])


def _is_unit_declaration(unit, declaration, *, table_header):
    if not _in_span(unit, declaration):
        return False
    text = declaration["quote"]
    offset = declaration["start"]
    prefix = text[:unit["start"]-offset].rstrip()
    suffix = text[unit["end"]-offset:].strip()
    if re.search(r"(?:单位|units?)\s*(?:均?为|[:：=])?$", prefix, re.IGNORECASE):
        return not suffix.strip("。；;")
    if re.search(r"(?:尺寸|标高).*(?:均以|以)$|\bdimensions\s+(?:are\s+)?in$", prefix, re.IGNORECASE):
        return suffix in ("", "计", "计。", "为单位", "为单位。")
    if table_header:
        for opening, closing in (("（", "）"), ("(", ")"), ("[", "]")):
            if prefix.endswith(opening) and suffix == closing:
                return bool(prefix[:-1].strip())
        return prefix.endswith("/") and bool(prefix[:-1].strip()) and not suffix
    return False


def _read_table(raw, evidence):
    if raw is None:
        return None
    table = _object(raw, "table")
    columns, rows = {}, {}
    for index, item in enumerate(_array(table.get("columns"), "table.columns")):
        path = f"table.columns[{index}]"
        item = _object(item, path)
        column_id = _text(item.get("id"), path+".id")
        _require(column_id not in columns, "DUPLICATE_ID", path)
        box = _bbox(item.get("bbox"), path+".bbox")
        if columns:
            _require(next(reversed(columns.values()))["local_bbox"]["x1"] <= box["x0"], "COLUMN_ORDER_OR_OVERLAP", path)
        header = evidence.span(item.get("header"), path+".header")
        _require(header is not None and _contains(box, header["local_bbox"]), "HEADER_OUTSIDE_COLUMN", path)
        columns[column_id] = {"id": column_id, "local_bbox": box, "bbox": evidence.map_box(box), "header": header}
    _require(bool(columns), "TABLE_COLUMNS_MISSING", "table.columns")
    for index, item in enumerate(_array(table.get("rows"), "table.rows")):
        path = f"table.rows[{index}]"
        item = _object(item, path)
        row_id = _text(item.get("id"), path+".id")
        _require(row_id not in rows, "DUPLICATE_ID", path)
        _require(type(item.get("row_index")) is int and item["row_index"] == index, "ROW_INDEX_ORDER", path)
        box = _bbox(item.get("bbox"), path+".bbox")
        if rows:
            _require(next(reversed(rows.values()))["local_bbox"]["y1"] <= box["y0"], "ROW_ORDER_OR_OVERLAP", path)
        cells = {}
        for cell_index, cell in enumerate(_array(item.get("cells"), path+".cells")):
            cell_path = f"{path}.cells[{cell_index}]"
            cell = _object(cell, cell_path)
            column_id = _text(cell.get("column_id"), cell_path+".column_id")
            _require(column_id in columns and column_id not in cells, "UNKNOWN_OR_REPEATED_COLUMN", cell_path)
            state = _enum(cell.get("state"), ("read", "blank", "unknown"), cell_path+".state")
            span = evidence.span(cell.get("content"), cell_path+".content")
            _require((state == "read") == (span is not None), "CELL_STATE_CONTRADICTION", cell_path)
            if span:
                _require(_contains(box, span["local_bbox"]) and
                         _contains(columns[column_id]["local_bbox"], span["local_bbox"]), "CELL_OUTSIDE_ROW_OR_COLUMN", cell_path)
            cells[column_id] = {"column_id": column_id, "state": state, "content": span}
        _require(cells.keys() == columns.keys(), "MISSING_PHYSICAL_CELLS", path)
        rows[row_id] = {"id": row_id, "row_index": index, "local_bbox": box, "bbox": evidence.map_box(box),
                        "cells": [cells[column] for column in columns]}
    return {"columns": list(columns.values()), "rows": list(rows.values())}


def _read_chain(raw, evidence):
    if raw is None:
        return None
    chain = _object(raw, "chain")
    direction = _enum(chain.get("direction"), ("horizontal", "vertical", "oblique", "unknown"), "chain.direction")
    endpoints = chain.get("endpoints")
    if endpoints is not None:
        _require(isinstance(endpoints, list) and len(endpoints) == 2 and all(
            isinstance(point, dict) and all(_coordinate(point.get(axis)) for axis in ("x", "y"))
            for point in endpoints), "INVALID_ENDPOINTS", "chain.endpoints")
        first, last = endpoints
        _require((first["x"], first["y"]) != (last["x"], last["y"]), "INVALID_ENDPOINTS", "chain.endpoints")
        if direction in ("horizontal", "vertical"):
            axis = "x" if direction == "horizontal" else "y"
            _require(first[axis] < last[axis], "CHAIN_DIRECTION_ORDER", "chain.endpoints")
    anchors = [evidence.span(item, f"chain.association_evidence[{index}]") for index, item in
               enumerate(_array(chain.get("association_evidence", []), "chain.association_evidence"))]
    _require(all(item is not None for item in anchors), "NULL_CHAIN_ANCHOR", "chain.association_evidence")
    segments = {}
    previous_position = None
    for index, item in enumerate(_array(chain.get("segments"), "chain.segments")):
        path = f"chain.segments[{index}]"
        item = _object(item, path)
        segment_id = _text(item.get("id"), path+".id")
        _require(segment_id not in segments, "DUPLICATE_ID", path)
        box = _bbox(item.get("bbox"), path+".bbox")
        center = {axis: (box[axis+"0"]+box[axis+"1"])/2 for axis in ("x", "y")}
        position = center["x"] if direction == "horizontal" else center["y"] if direction == "vertical" else None
        if direction == "oblique" and endpoints:
            position = sum(center[axis]*(endpoints[1][axis]-endpoints[0][axis]) for axis in ("x", "y"))
        if position is not None and previous_position is not None:
            _require(position > previous_position, "CHAIN_SEGMENT_ORDER", path)
        previous_position = position
        label = evidence.span(item.get("label"), path+".label")
        if label:
            _require(_contains(box, label["local_bbox"]), "LABEL_OUTSIDE_SEGMENT", path)
        segments[segment_id] = {"id": segment_id, "segment_index": index, "local_bbox": box,
                                "bbox": evidence.map_box(box), "label": label}
    return {"direction": direction, "local_endpoints": deepcopy(endpoints),
            "endpoints": [evidence.map_point(point) for point in endpoints] if endpoints else None,
            "association_evidence": anchors, "segments": list(segments.values())}


def _measurement(raw, path, evidence, table, chain, *, field_errors=None):
    raw = _object(raw, path)
    measurement_id = _text(raw.get("id"), path+".id")
    basis = _enum(raw.get("relation_basis"), ("same_phrase", "table_row", "chain_segment", "unresolved"), path+".relation_basis")
    origin = _enum(raw.get("unit_origin"), ("explicit", "inherited", "unknown"), path+".unit_origin")
    _require(all(key in raw for key in ("literal", "value", "unit", "comparator", "property", "object")),
             "MISSING_MEASUREMENT_FIELDS", path)
    literal = evidence.span(raw["literal"], path+".literal")
    _require(literal is not None, "MISSING_LITERAL", path)

    def read_span(field, **options):
        source = raw.get(field)
        location = path+"."+field
        try:
            if field == "comparator" and source is not None:
                source = _object(source, location)
                quote = _text(source.get("quote", source.get("text")), location+".quote")
                _require(quote in COMPARATORS, "INVALID_COMPARATOR", location+".quote")
            return evidence.span(source, location, **options)
        except LayoutContextError as error:
            if field_errors is None:
                raise
            field_errors.append(error)
            return None

    value = read_span("value", within=literal)
    comparator = read_span("comparator", within=literal)
    preferred = literal if basis == "same_phrase" else None
    subject = read_span("object", preferred=preferred)
    prop = read_span("property", preferred=preferred)
    unit_relation = read_span("unit_relation")
    separate_unit = raw.get("unit_column_id") is not None
    _require(not separate_unit or (basis == "table_row" and origin == "explicit"), "UNLINKED_UNIT_COLUMN", path)
    anchor = unit_relation if origin == "inherited" else literal if origin == "explicit" and not separate_unit else None
    unit = read_span("unit", within=anchor)
    if field_errors:
        return None
    _require((origin == "unknown") == (unit is None), "UNIT_ORIGIN_CONTRADICTION", path)
    _require((origin == "inherited") == (unit_relation is not None), "UNIT_DECLARATION_REQUIRED", path)
    if unit_relation:
        _require(_is_unit_declaration(unit, unit_relation, table_header=basis == "table_row"),
                 "UNIT_DECLARATION_REQUIRED", path)
    if value:
        _validate_numeric(value, comparator, unit if origin == "explicit" and not separate_unit else None, evidence, path)

    if basis == "same_phrase":
        _require(evidence.region["region_type"] != "table" and prop and subject and
                 evidence.same_paragraph((literal, prop, subject)), "UNSUPPORTED_PHRASE_RELATION", path)
    conditions = []
    if "conditions" in raw:
        _require(evidence.region["region_type"] == "notes", "UNEXPECTED_CONDITIONS", path)
        for index, reference in enumerate(_array(raw["conditions"], path+".conditions")):
            location = f"{path}.conditions[{index}]"
            condition = evidence.span(reference, location)
            _require(condition is not None and evidence.same_paragraph((literal, condition)),
                     "CONDITION_OUTSIDE_PARAGRAPH", location)
            _require(not any(previous["line_id"] == condition["line_id"] and
                             previous["start"] < condition["end"] and condition["start"] < previous["end"]
                             for previous in (literal, *conditions)), "OVERLAPPING_CONDITION", location)
            conditions.append(condition)
    if basis == "table_row":
        _validate_table_measurement(raw, table, literal, prop, subject, unit, unit_relation, path)
    if basis == "chain_segment":
        segment_id = _text(raw.get("segment_id"), path+".segment_id")
        segment = next((item for item in chain["segments"] if item["id"] == segment_id), None) if chain else None
        _require(segment and _in_span(literal, segment["label"]), "UNSUPPORTED_CHAIN_SEGMENT", path)
        _require(chain["direction"] != "unknown" and (chain["endpoints"] or chain["association_evidence"]),
                 "CHAIN_GEOMETRY_OR_ASSOCIATION_REQUIRED", path)
        if unit_relation:
            _require(any(_in_span(unit_relation, anchor) for anchor in chain["association_evidence"]),
                     "UNIT_OUTSIDE_CHAIN_ASSOCIATION", path)
        for span in (subject, prop):
            if span:
                _require(any(_in_span(span, anchor) for anchor in chain["association_evidence"]),
                         "UNSUPPORTED_CHAIN_ASSOCIATION", path)
    for span in (subject, prop):
        if span:
            _require(not SCALAR.fullmatch(span["quote"].strip()), "BARE_NUMBER_ASSOCIATION", path)
    object_ref = None
    if subject and basis != "unresolved":
        object_ref = [evidence.region["id"], subject["line_id"], subject["start"], subject["end"]]
    return {"id": measurement_id, "region_id": evidence.region["id"], "literal": literal, "value": value,
            "unit": unit, "comparator": comparator, "property": prop, "object": subject,
            "unit_origin": origin, "unit_relation": unit_relation, "relation_basis": basis,
            "row_id": raw.get("row_id"), "column_id": raw.get("column_id"), "segment_id": raw.get("segment_id"),
            "unit_column_id": raw.get("unit_column_id"), "object_ref": object_ref,
            "relation_status": "unresolved" if basis == "unresolved" else "candidate", "identity_confirmed": False,
            **({"conditions": conditions} if "conditions" in raw else {})}


def _validate_numeric(value, comparator, inline_unit, evidence, path):
    _require(bool(SCALAR.fullmatch(value["quote"])), "NONSCALAR_VALUE", path)
    text = evidence.lines[value["line_id"]]["text"]
    start, end = value["start"], value["end"]
    before, after = text[:start].rstrip(), text[end:]
    if comparator:
        _require(comparator["end"] <= start and not text[comparator["end"]:start].strip(),
                 "COMPARATOR_NOT_ADJACENT", path+".comparator")
        prefix = text[:comparator["start"]].rstrip()
        _require(not prefix or prefix[-1] not in QUALIFIERS,
                 "COMPARATOR_TOKEN_TRUNCATED", path+".comparator")
    else:
        _require(not before or before[-1] not in QUALIFIERS, "OMITTED_VISIBLE_COMPARATOR", path+".comparator")
    left = text[start-1] if start else ""
    _require(not left or not (left.isdigit() or left in ".#×+-/" or (left.isascii() and left.isalpha())),
             "NUMERIC_TOKEN_TRUNCATED", path)
    if inline_unit:
        _require(end <= inline_unit["start"] and not text[end:inline_unit["start"]].strip(), "UNIT_NOT_ADJACENT", path)
        after = text[inline_unit["end"]:]
    right = after[:1]
    _require(not right or not (right.isdigit() or right in ".#×/" or (right.isascii() and right.isalpha())),
             "NUMERIC_TOKEN_TRUNCATED", path)


def _validate_table_measurement(raw, table, literal, prop, subject, unit, unit_relation, path):
    row_id = _text(raw.get("row_id"), path+".row_id")
    column_id = _text(raw.get("column_id"), path+".column_id")
    row = next((item for item in table["rows"] if item["id"] == row_id), None) if table else None
    _require(row is not None, "UNKNOWN_PHYSICAL_ROW", path)
    cells = {cell["column_id"]: cell["content"] for cell in row["cells"]}
    _require(_in_span(literal, cells.get(column_id)), "LITERAL_OUTSIDE_TABLE_CELL", path)
    header = next(item["header"] for item in table["columns"] if item["id"] == column_id)
    if prop:
        _require(_in_span(prop, header) or _in_span(prop, cells[column_id]), "PROPERTY_OUTSIDE_COLUMN", path)
    if subject:
        _require(any(_in_span(subject, cell) for cell in cells.values()), "OBJECT_OUTSIDE_PHYSICAL_ROW", path)
    if unit_relation:
        _require(_in_span(unit_relation, header), "UNIT_OUTSIDE_COLUMN_HEADER", path)
    if raw.get("unit_column_id") is not None:
        unit_col = _text(raw["unit_column_id"], path+".unit_column_id")
        _require(unit_col != column_id and _in_span(unit, cells.get(unit_col)), "UNIT_OUTSIDE_PHYSICAL_ROW", path)


def validate_region(payload, region, image_size):
    """Resolve all exact source quotes and map evidence into the full-page frame."""
    raw = _object(payload, "region_response")
    region = _object(region, "region")
    region_id = _text(region.get("id"), "region.id")
    kind = _enum(region.get("region_type"), REGION_TYPES, "region.region_type")
    context_status = _enum(raw.get("context_status"), BOUNDARY_STATES, "context_status")
    evidence = _RegionEvidence(region, image_size, raw)
    _require(context_status != "complete" or bool(evidence.lines), "EMPTY_COMPLETE_CONTEXT", "lines")
    _require(raw.get("table") is None or kind == "table", "UNEXPECTED_TABLE", "table")
    _require(raw.get("chain") is None or kind == "dimension_chain", "UNEXPECTED_CHAIN", "chain")
    table = _read_table(raw.get("table"), evidence)
    chain = _read_chain(raw.get("chain"), evidence)
    if context_status == "complete":
        _require(kind != "table" or (table and table["rows"]), "TABLE_CONTEXT_REQUIRED", "table")
        _require(kind != "dimension_chain" or (chain and chain["segments"]), "CHAIN_CONTEXT_REQUIRED", "chain")
    measurements = {}
    for index, item in enumerate(_array(raw.get("measurements"), "measurements")):
        path = f"measurements[{index}]"
        measurement = _measurement(item, path, evidence, table, chain)
        _require(measurement["id"] not in measurements, "DUPLICATE_ID", path)
        measurements[measurement["id"]] = measurement
    lines = list(evidence.lines.values())
    return {"version": VERSION, "region_id": region_id, "region_type": kind,
            "parent_region": deepcopy(region), "context_status": context_status,
            "context_text": "\n".join(line["text"] for line in lines), "lines": lines,
            "measurements": list(measurements.values()), "table": table, "chain": chain,
            "quality_status": "unassessed"}


def _precise_overlap(left, right):
    if left["bbox_precision"] != "span" or right["bbox_precision"] != "span":
        return False
    first, last = left["bbox"], right["bbox"]
    intersection = (max(0, min(first["x1"], last["x1"])-max(first["x0"], last["x0"])) *
                    max(0, min(first["y1"], last["y1"])-max(first["y0"], last["y0"])))
    minimum = min((box["x1"]-box["x0"])*(box["y1"]-box["y0"]) for box in (first, last))
    return intersection >= .7*minimum


def summarize(plan, results_by_region_id):
    """Preserve every reading and declare every unfinished scope explicitly."""
    results = _object(results_by_region_id, "region_results")
    regions = plan["regions"]
    scheduled = {row["id"] for row in regions if row["state"] == "planned"}
    _require(results.keys() <= scheduled, "UNSCHEDULED_RESULT", "region_results")
    gaps = [{"scope": "whole_page", "state": "unknown", "reason": "whole_page_coverage_unverified"}]
    gaps.extend(deepcopy(plan["unlocated"]))
    readings, measurements, conflicts = [], [], []
    for region in regions:
        region_id = region["id"]
        result = results.get(region_id)
        if result is None:
            gaps.append({"region_id": region_id, "state": "unknown", "reason":
                         "not_scheduled" if region["state"] != "planned" else "missing_or_failed_region"})
            continue
        _require(isinstance(result, dict) and result.get("version") == VERSION and
                 result.get("parent_region") == region, "RESULT_REGION_MISMATCH", "region_results")
        readings.append(deepcopy(result))
        if result["context_status"] != "complete" or region["boundary_status"] != "complete":
            gaps.append({"region_id": region_id, "state": "unknown", "reason": "incomplete_or_uncertain_context"})
        table, chain = result["table"], result["chain"]
        if table and any(cell["state"] == "unknown" for row in table["rows"] for cell in row["cells"]):
            gaps.append({"region_id": region_id, "state": "unknown", "reason": "unreadable_table_cells"})
        if chain and any(segment["label"] is None for segment in chain["segments"]):
            gaps.append({"region_id": region_id, "state": "unknown", "reason": "unreadable_chain_segments"})
        for measurement in result["measurements"]:
            for prior in measurements:
                if prior["region_id"] != region_id and _precise_overlap(prior["literal"], measurement["literal"]):
                    keys = ("literal", "value", "unit", "comparator", "property", "object")
                    changes = [key for key in keys if (prior[key] or {}).get("quote") != (measurement[key] or {}).get("quote")]
                    if changes:
                        conflicts.append({"left": [prior["region_id"], prior["id"]],
                                          "right": [region_id, measurement["id"]], "differing_fields": changes,
                                          "state": "candidate_conflict"})
            measurements.append(deepcopy(measurement))
    return {"version": VERSION, "status": "partial", "quality_status": "unassessed", "coverage": "unknown",
            "whole_complete": False, "scheduled_regions": len(scheduled), "validated_regions": len(readings),
            "coverage_gaps": gaps, "regions": readings, "measurements": measurements, "conflicts": conflicts}
