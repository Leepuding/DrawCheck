"""Opt-in source-page locator for complete drawing-view contexts."""
from __future__ import annotations

from math import ceil, floor

from backend.extract.layout_context import (
    BOUNDARY_STATES, _array, _bbox, _contains, _enum, _object, _require, _size, _text,
)


VERSION = "layout-context-v2"
REGION_TYPES = ("notes", "table", "drawing_view", "title", "other")
CLASSIFICATION_STATES = ("supported", "classification_uncertain")

LOCATOR_SYSTEM_PROMPT = """你只在原审查图上定位版面，输出一个 JSON 对象。先识别图上完整的结构，再画框。所有坐标以当前原图左上为原点，归一化为0到1。根据实际可见结构分类，保留分类不确定、边界不清和未定位的内容。"""

LOCATOR_USER_PROMPT = """逐个定位本页上可区分的说明、材料表、绘图视图、图签和其他版面区域。

notes：连同标题、完整的说明段和相邻续行定位。与某一视图直接关联的就地注释也要保留在该视图的上下文中。
table：材料或设备表须看见实际列标题及至少一条物理数据行。框住表头、全部可见物理行和附属表注，保留重复行与空白格所在的物理行。仅有网格或字段标签不足以认定为表。
drawing_view：以完整的平面、剖面、立面或详图视图为单位。bbox框住该视图的构件主体和图形轮廓。context_bbox在此基础上向上、下、左、右检查并纳入所有属于同一视图的可见尺寸线、尺寸界线、端点、数值、引线及引注、剖切符号、视图名和比例。视图内的尺寸也要保留。横、竖、斜向尺寸属于同一视图时一起留在一个框内。不得把孤立数字或单段尺寸链另框成一个视图。按实际独立视图分框；与本视图无关的说明块、其他视图、图签无需纳入。
title：须在同一身份栏内看见图号字段和图名、比例、日期等配套字段。按实际字段确认后框住整个身份栏。视图名、公司名、审批栏、图纸目录、资质证号单独出现时都不足以认定为图签。
other：用来定位资质、许可、印章、目录等其他可见结构。资质或许可编号及印章即使紧贴图签，也按独立区定位。仅凭角落位置、方框、网格、印章外形无法确定类型。

结构证据清楚时classification_status填supported；区域可框但证据不足时保留框并填classification_uncertain，说明classification_reason。可确定候选类型就填该类型，否则用other。边界看不清、可能截断或与相邻内容难区分时，boundary_status填partial或unknown。complete只表示当前区域的观察边界清楚。不能定位的可见内容保留在unlocated并说明原因。

按以下合成示例的JSON格式输出。示例坐标和原因只展示格式，按原图独立定位。context_bbox必须完整包含bbox。区域可因共享可见证据而重叠。只做定位，不输出整页覆盖结论。
{"regions":[{"region_type":"drawing_view","bbox":{"x0":0.17,"y0":0.32,"x1":0.33,"y1":0.48},"context_bbox":{"x0":0.1,"y0":0.25,"x1":0.4,"y1":0.55},"classification_status":"supported","boundary_status":"complete"},{"region_type":"other","bbox":{"x0":0.57,"y0":0.14,"x1":0.8,"y1":0.22},"context_bbox":{"x0":0.56,"y0":0.13,"x1":0.81,"y1":0.23},"classification_status":"classification_uncertain","classification_reason":"无法辨认栏内字段","boundary_status":"unknown"}],"unlocated":[{"region_type":"other","reason":"图面内容重叠，无法确定独立边界"}]}
"""


def plan_regions(payload, image_size, *, max_regions=None):
    """Freeze validated geometry while preserving unlocated and uncertain regions."""
    width, height = _size(image_size)
    _require(max_regions is None or (type(max_regions) is int and max_regions >= 0),
             "INVALID_REGION_LIMIT", "max_regions")
    raw = _object(payload, "locator")
    by_type = {kind: [] for kind in REGION_TYPES}
    for index, item in enumerate(_array(raw.get("regions"), "locator.regions")):
        path = f"locator.regions[{index}]"
        item = _object(item, path)
        type_fields = _region_type_fields(item.get("region_type"), path+".region_type")
        box = _bbox(item.get("bbox"), path+".bbox")
        context = _bbox(item.get("context_bbox"), path+".context_bbox")
        _require(_contains(context, box), "CONTEXT_EXCLUDES_REGION", path)
        status = _enum(item.get("classification_status"), CLASSIFICATION_STATES, path+".classification_status")
        region = {**type_fields, "bbox": box, "context_bbox": context,
                  "pixel_box": [floor(context["x0"]*width), floor(context["y0"]*height),
                                ceil(context["x1"]*width), ceil(context["y1"]*height)],
                  "boundary_status": _enum(item.get("boundary_status"), BOUNDARY_STATES, path+".boundary_status"),
                  "classification_status": status}
        if status == "classification_uncertain":
            region["classification_reason"] = _text(item.get("classification_reason"), path+".classification_reason")
        by_type[region["region_type"]].append(region)

    for rows in by_type.values():
        rows.sort(key=_region_order)
    # Keep the v1 type rotation and page order. Uncertain regions retain IDs
    # and geometry without consuming a slot for supported regions.
    regions = [rows[index] for index in range(max(map(len, by_type.values())))
               for rows in by_type.values() if index < len(rows)]
    supported = 0
    for index, region in enumerate(regions):
        state = region["classification_status"]
        if state == "supported":
            state = "planned" if max_regions is None or supported < max_regions else "not_scheduled"
            supported += 1
        region.update(id=f"region-{index+1}", state=state)

    unlocated = []
    for index, item in enumerate(_array(raw.get("unlocated", []), "locator.unlocated")):
        path = f"locator.unlocated[{index}]"
        item = _object(item, path)
        unlocated.append({**_region_type_fields(item.get("region_type"), path+".region_type"),
                          "reason": _text(item.get("reason"), path+".reason"), "state": "unknown"})
    return {"version": VERSION, "image_size": [width, height], "regions": regions,
            "unlocated": unlocated, "coverage": "unknown", "whole_complete": False}


def _region_type_fields(value, path):
    if value == "note":
        return {"region_type": "notes", "source_region_type": "note"}
    return {"region_type": _enum(value, REGION_TYPES, path)}


def _region_order(region):
    geometry = tuple(region[field][key] for field in ("bbox", "context_bbox") for key in ("y0", "x0", "y1", "x1"))
    return (*geometry, region["boundary_status"], region["classification_status"],
            region.get("classification_reason", ""), region.get("source_region_type", ""))
