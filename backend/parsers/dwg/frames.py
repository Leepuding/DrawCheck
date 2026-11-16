"""布局与 TK-* 图框启发；文字按实际渲染笔画定位到单元左上角坐标。"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
import math
from pathlib import Path

from backend.parsers.base import ParsedText
from backend.store.models import BBox, Source


@dataclass
class Frame:
    layout: object
    name: str
    bounds: tuple[float, float, float, float]
    texts: list[ParsedText]
    native_measurements: tuple[dict, ...] = ()
    native_signals: tuple[dict, ...] = ()
    native_connections: tuple[dict, ...] = ()


def valid_bounds(values):
    return (len(values) == 4 and all(math.isfinite(v) for v in values)
            and values[2] > values[0] and values[3] > values[1])


def normalized_box(bounds, text_bounds):
    x0, y0, x1, y1 = bounds
    a, b, c, d = text_bounds
    clamp = lambda v: max(0.0, min(1.0, v))
    return BBox(clamp((a - x0) / (x1 - x0)), clamp((y1 - d) / (y1 - y0)),
                clamp((c - x0) / (x1 - x0)), clamp((y1 - b) / (y1 - y0)))


def _bounds(entities, cache):
    from ezdxf import bbox
    from ezdxf.lldxf.const import DXFStructureError

    entities = list(entities)
    try:
        box = bbox.extents(entities, fast=True, cache=cache)
    except DXFStructureError:
        box = bbox.extents(_safe_decompose(entities), fast=True, cache=cache)
    if not box.has_data:
        return None
    values = (box.extmin.x, box.extmin.y, box.extmax.x, box.extmax.y)
    return values if valid_bounds(values) else None


def missing_block_references(doc):
    """Keep malformed INSERTs in the source inventory; never invent their geometry."""
    return [entity for entity in doc.entitydb.values()
            if entity.dxftype() == "INSERT" and entity.block() is None]


def _safe_decompose(entities):
    """Like ezdxf's recursive_decompose, but one absent block cannot abort a layout.

    The caller reports every absent reference separately. Only an INSERT with no
    definition is unexpandable; siblings and attached attributes are retained.
    No document entity is deleted or edited.
    """
    from ezdxf.entities import Insert
    from ezdxf.protocols import SupportsVirtualEntities, virtual_entities

    for entity in entities:
        if isinstance(entity, Insert):
            if entity.mcount > 1:
                yield from _safe_decompose(entity.multi_insert())
            else:
                yield from entity.attribs
                block = entity.block()
                if block is not None:
                    yield from _safe_decompose(virtual_entities(entity))
        elif isinstance(entity, SupportsVirtualEntities):
            yield from _safe_decompose(virtual_entities(entity))
        else:
            yield entity


# GB/T 14689 基本幅面（长边, 短边），单位 mm；按比例因子匹配图块外包框。
_BASE_SHEETS = {"A0": (1189, 841), "A1": (841, 594), "A2": (594, 420), "A3": (420, 297), "A4": (297, 210)}
_SHEET_TOLERANCE = 0.02


def _sheet_patterns():
    """基本幅面 + 常见加长幅面（A0～A2 长边按 1/4 递增，A3/A4 短边倍数加长）。"""
    patterns = []
    for name, (long, short) in _BASE_SHEETS.items():
        patterns.append((name, long, short))
        if name in {"A0", "A1", "A2"}:
            patterns.extend((f"{name}+{n}/4", long * (1 + n / 4), short) for n in range(1, 9))
        else:
            patterns.extend((f"{name}x{m}", short * m, long) for m in range(3, 10))
    return patterns


_SHEETS = _sheet_patterns()


def match_sheet(width, height):
    """外包框长宽比与标准/加长幅面一致时返回 (幅面名, 比例因子)，否则 None。"""
    long, short = max(width, height), min(width, height)
    if short <= 0:
        return None
    matches = []
    for name, sheet_long, sheet_short in _SHEETS:
        factor = short / sheet_short
        if abs(long / factor - sheet_long) <= sheet_long * _SHEET_TOLERANCE:
            matches.append((abs(math.log(factor)), name, factor))
    # A 系列各幅面长宽比相同，取比例因子最接近 1:1 的幅面名（仅用于原因说明）。
    return (min(matches)[1], min(matches)[2]) if matches else None


def _center_inside(box, bounds):
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return bounds[0] <= cx <= bounds[2] and bounds[1] <= cy <= bounds[3]


def _area(box):
    return (box[2] - box[0]) * (box[3] - box[1])


def _standard_sheet_candidates(layout, tk_candidates, cache):
    """布局已有 TK-* 图框时，补充识别框外符合标准图幅的非 TK-* 图块（如目录页、A4 说明页）。

    只作补充：中心落在 TK-* 框内的、面积远小于/大于已识别图框的、彼此嵌套的都不算。
    """
    tk_boxes = [box for _, box in tk_candidates]
    areas = [_area(box) for box in tk_boxes]
    low, high = 0.2 * min(areas), 4 * max(areas)
    found = []
    for entity in layout:
        if entity.dxftype() != "INSERT" or entity.dxf.name.upper().startswith("TK-"):
            continue
        box = _bounds([entity], cache)
        if not box or not low <= _area(box) <= high:
            continue
        sheet = match_sheet(box[2] - box[0], box[3] - box[1])
        if sheet is None or any(_center_inside(box, tk) for tk in tk_boxes):
            continue
        found.append((entity.dxf.name, box, sheet[0]))
    found.sort(key=lambda item: -_area(item[1]))
    kept = []
    for name, box, sheet in found:
        if not any(_center_inside(box, other) for _, other, _ in kept):
            kept.append((name, box, sheet))
    return kept


def find_frames(doc):
    from ezdxf import bbox
    from .dimension_marks import read_shared_dimensions
    from .engineering import (frame_dimensions, frame_elevations, frame_signals, frame_connections,
                              native_dimensions, native_level_strokes, native_signals, native_connections)
    from .render import rendered_text_bounds

    reasons = []
    frames = []
    counts = Counter()
    for entity in missing_block_references(doc):
        point = entity.dxf.insert
        reasons.append(f"INSERT {entity.dxf.handle} 缺少块定义（块名「{entity.dxf.name or '空'}」，"
                       f"插入点 {point.x:g},{point.y:g}），覆盖不完整，该引用的几何范围及受影响图框无法核实")
    for block in doc.blocks:
        # LibreDWG also writes internal anonymous-block aliases ("*A4") and
        # "Acad:XRef" into group code 1. Those strings alone do not establish an
        # external DWG dependency: BLOCK flags 4/8 identify xref/overlay blocks.
        if block.block.dxf.flags & 12:
            reasons.append("存在外部参照，未加载外部文件，仅部分提取")
    for entity in doc.entitydb.values():
        if entity.dxftype() in {"ACAD_PROXY_ENTITY", "ACAD_PROXY_OBJECT"}:
            reasons.append("存在代理对象，仅部分提取")
        if entity.dxftype() in {"3DSOLID", "BODY", "REGION", "UNDERLAY", "ACAD_TABLE"}:
            reasons.append("存在渲染器未完整支持的实体，仅部分提取")
    cache = bbox.Cache()
    for layout in doc.layouts:
        if not len(layout):
            continue
        counts.update(e.dxftype() for e in layout)
        candidates = []
        for entity in layout:
            if entity.dxftype() == "INSERT" and entity.dxf.name.upper().startswith("TK-"):
                box = _bounds([entity], cache)
                if box:
                    candidates.append((entity.dxf.name, box))
        pending_confirm = set()
        if candidates:
            # M2：非 TK-* 但符合标准图幅的图块纳入单元；单元名自带「图框待确认」，原因用裁定文案。
            for name, box, sheet in _standard_sheet_candidates(layout, candidates, cache):
                candidates.append((name, box))
                pending_confirm.add(name)
                # 文件级摘要保留幅面提示；裁定句式供目录/原因展示。
                reasons.append(
                    f"块名不是常见图框（当前为：{name}），请核对是不是一张图"
                    f"（按标准图幅 {sheet} 识别）"
                )
        if not candidates:
            box = _bounds(layout, cache)
            if not box:
                reasons.append(f"布局 {layout.name} 无可用二维范围，无法生成单元")
                continue
            candidates = [(layout.name, box)]
            reasons.append(f"布局 {layout.name} 无可靠图框，按整布局部分提取")
        texts = []
        try:
            text_entities = [entity for entity in _safe_decompose(layout)
                             if entity.dxftype() in {"TEXT", "MTEXT", "ATTRIB"}]
            text_boxes, text_diagnostics = rendered_text_bounds(doc, text_entities)
            for diagnostic in text_diagnostics:
                if diagnostic.get("code") in {"TEXT_RENDER_FAILED", "TEXT_NO_RENDERED_BOUNDS"}:
                    reasons.append("部分 CAD 文字精确定位未完成：" + diagnostic.get("reason", diagnostic["code"]))
            for entity, box in zip(text_entities, text_boxes):
                kind = entity.dxftype()
                text = entity.plain_text() if kind == "MTEXT" else entity.dxf.text
                if text.strip() and box:
                    texts.append((text, box))
                elif text.strip():
                    reasons.append("部分 CAD 文字无法定位，未生成虚构文字框")
        except Exception as exc:
            reasons.append(f"部分块或文字展开失败：{exc}")
        dimensions = []
        try:
            dimensions.extend(native_dimensions(layout, cache))
        except Exception as exc:
            reasons.append(f"部分 CAD 尺寸对象读取失败：{exc or type(exc).__name__}")
        try:
            shared_dimensions = read_shared_dimensions(layout, [bounds for _, bounds in candidates])
        except Exception as exc:
            shared_dimensions = {}
            reasons.append(f"部分 CAD 底图尺寸标注读取失败：{exc or type(exc).__name__}")
        signals = list(native_signals(layout, cache))
        connections = []
        try:
            connections = list(native_connections(layout, cache))
        except Exception as exc:
            reasons.append(f"部分 CAD 接线几何读取失败：{exc or type(exc).__name__}")
        level_strokes = list(native_level_strokes(layout))
        assigned = [False] * len(texts)
        for name, bounds in candidates:
            extracted = []
            for index, (text, box) in enumerate(texts):
                if _center_inside(box, bounds):
                    assigned[index] = True
                    extracted.append(ParsedText(text, Source.CAD_TEXT, normalized_box(bounds, box)))
            # N1：非 TK 单元名称自带标记，便于目录展示（ParsedUnit 无额外字段，不改 base.py）。
            label = f"{layout.name} / {name}（图框待确认）" if name in pending_confirm else f"{layout.name} / {name}"
            readings = (frame_dimensions(dimensions, bounds) + frame_elevations(level_strokes, bounds, extracted)
                        + shared_dimensions.get(bounds, []))
            frames.append(Frame(layout, label, bounds, extracted, tuple(readings), tuple(frame_signals(signals, bounds)),
                                tuple(frame_connections(connections, bounds))))
        outside = assigned.count(False)
        if outside:
            # M2：不在任何单元内的文字不得静默丢弃，至少计数写明。
            reasons.append(f"布局 {layout.name} 有 {outside} 条 CAD 文字不在已识别图框内，未拆入任何单元")
    return frames, list(dict.fromkeys(reasons)), dict(counts)


def read_json_fallback(path: Path):
    """只信任合法 JSON 的实体统计；块变换未知时不猜测图纸坐标与图框。

    LibreDWG JSON 可能含非 UTF-8 字节（样图文字编码），按 UTF-8 容错解码后再解析。
    """
    # Spike 用字节正则统计；产品路径仍解析 JSON，但不得因个别非法字节整文件失败。
    raw = path.read_bytes()
    replaced = 0
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = raw.decode("utf-8", errors="replace")
        replaced = content.count("\ufffd")
    # 尽早释放原始字节，降低大 JSON（~200 MB）解析时的峰值内存。
    del raw
    data = json.loads(content)
    del content
    objects = data.get("OBJECTS", []) if isinstance(data, dict) else []
    if not isinstance(objects, list):
        raise ValueError("LibreDWG JSON 的 OBJECTS 不是列表")
    entities = [o for o in objects if isinstance(o, dict) and isinstance(o.get("entity"), str)]
    counts = Counter(o["entity"] for o in entities)
    stats = {"source": "libredwg_json", "entity_total": len(entities),
             "entities": dict(counts), "layout_count": sum(
                 isinstance(o, dict) and o.get("object") == "LAYOUT" for o in objects),
             "suspected_titleblocks": sorted({o["name"] for o in objects
                 if isinstance(o, dict) and isinstance(o.get("name"), str)
                 and o["name"].upper().startswith("TK-")})}
    reasons = ["JSON 兜底仅部分提取；未重建完整几何，无法生成完整预览",
               "JSON 块归属与变换未经验证，未将块内坐标冒充单元坐标"]
    if replaced:
        reasons.append(f"JSON 含非 UTF-8 字节，已按替换字符处理约 {replaced} 处，相关文字可能缺字")
    # M1：LibreDWG 0.13.3 JSON 中模型空间实体用 entmode=2 表示且不写 ownerhandle（DWG 规范：
    # entmode 0=由 ownerhandle 指明归属（块内），1=图纸空间，2=模型空间）。ownerhandle 指向
    # *Model_Space 的旧判断保留作补充。块内文字（含图框属性 ATTRIB）坐标需块变换，样本 JSON 句柄
    # 大量重复，归属不可靠，故只计数不定位。
    def handle(value):
        if isinstance(value, list) and value:
            return value[-1]
        return value

    model_handles = {handle(o.get("handle")) for o in objects if isinstance(o, dict)
                     and o.get("object") == "BLOCK_HEADER"
                     and str(o.get("name", "")).upper() in {"*MODEL_SPACE", "$MODEL_SPACE"}}
    model_handles.discard(None)
    positioned = []
    skipped = Counter()
    for entity in entities:
        kind = entity["entity"]
        if kind not in {"TEXT", "MTEXT", "ATTRIB"}:
            continue
        text = entity.get("text_value") or entity.get("text") or ""
        if not isinstance(text, str) or not text.strip():
            continue
        if kind == "MTEXT":
            from ezdxf.tools.text import plain_mtext

            text = plain_mtext(text)
        point = entity.get("ins_pt", entity.get("insertion_pt"))
        owner = handle(entity.get("ownerhandle"))
        if entity.get("entmode") == 2 or (owner is not None and owner in model_handles):
            try:
                x, y = (float(point["x"]), float(point["y"])) if isinstance(point, dict) else map(float, point[:2])
            except (TypeError, ValueError, KeyError, IndexError):
                skipped["模型空间但无插入点"] += 1
                continue
            if text.strip() and math.isfinite(x) and math.isfinite(y):
                positioned.append((text, x, y))
            else:
                skipped["模型空间但无插入点"] += 1
        elif entity.get("entmode") == 1:
            skipped["图纸空间"] += 1
        else:
            skipped["块内（含图框属性）"] += 1
    stats["cad_text_positioned"] = len(positioned)
    stats["cad_text_skipped"] = dict(skipped)
    texts = []
    if positioned:
        xs, ys = [p[1] for p in positioned], [p[2] for p in positioned]
        bounds = (min(xs), min(ys), max(xs) + 1, max(ys) + 1)
        for text, x, y in positioned:
            texts.append(ParsedText(text, Source.CAD_TEXT, normalized_box(bounds, (x, y, x, y))))
        reasons.append(f"JSON 兜底提取模型空间 CAD 文字 {len(positioned)} 条；文字框为插入点，按已提取文字范围归一化，非完整图纸尺寸")
    else:
        reasons.append("JSON 兜底未找到可定位的模型空间 CAD 文字")
    if skipped:
        detail = "、".join(f"{k} {v} 条" for k, v in skipped.items())
        reasons.append(f"JSON 中另有文字未定位、未纳入单元：{detail}（块变换/归属未验证）")
    if any("PROXY" in k or "UNKNOWN" in k for k in counts):
        reasons.append("JSON 存在代理或未知对象，仅部分提取")
    return stats, texts, reasons
