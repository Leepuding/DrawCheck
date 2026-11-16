"""Read each fixed image strip independently before interpreting relationships."""
from __future__ import annotations

import json
import re

from .layout_context import (
    COMPARATORS, LayoutContextError, _RegionEvidence, _measurement, _region_prompt_example, validate_region,
)
from .line_regions import bind_note_lines


TRANSCRIBE_SYSTEM_PROMPT = """你逐字转录带固定ID的独立文字行。第一张图是原裁剪背景，第二张图是本次转录的行拼版。只读取第二张图每个ID旁的独立原像素行条。每条只写该行所见文字。保持原字、符号和行边界。只输出一个JSON对象。"""
RELATIONS_SYSTEM_PROMPT = """你从固定的逐行转录中提取说明关系，只输出一个JSON对象。所有字段引用来自输入行的逐字原文。输入行、段落和字符保持原样。"""
_LINE_BREAKS = frozenset("\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029")
_DIGIT = r"[0-9０-９]"
_LABEL = rf"(?:{_DIGIT}+|[零〇一二三四五六七八九十百千]+)"
# A dot immediately followed by another digit can be a decimal in a continuation.
_NUMBERED_PARAGRAPH = re.compile(
    rf"^\s*(?:[（(]\s*{_LABEL}\s*[）)]|{_LABEL}[、）)]|{_LABEL}[.．](?!\s*{_DIGIT}))"
)
_AMBIGUOUS_PARAGRAPH = re.compile(
    rf"^\s*(?:[（(]\s*{_LABEL}|{_LABEL}[.．]\s+{_DIGIT}|"
    rf"{_DIGIT}+(?:[.．]{_DIGIT}+)+[、.．）)]|[•●▪]|[-–—]\s+)"
)


def _expected_ids(line_ids):
    if not isinstance(line_ids, (list, tuple)) or not line_ids:
        raise LayoutContextError("INVALID_FIXED_LINE_IDS", "line_ids")
    if any(not isinstance(value, str) or not value.strip() for value in line_ids):
        raise LayoutContextError("INVALID_FIXED_LINE_IDS", "line_ids")
    if len(set(line_ids)) != len(line_ids):
        raise LayoutContextError("INVALID_FIXED_LINE_IDS", "line_ids")
    return list(line_ids)


def build_transcribe_prompt(line_ids):
    ids = _expected_ids(line_ids)
    example = {"lines": [{"id": "demo-A", "text": "示例文字一行"}, {"id": "demo-B", "text": None}]}
    return f"""按第二张图的独立行条转录。行条外的ID只用于对照。相邻行条即使属于同一段也各自保留。看不清的整行填null。保持原文标点与单位，直接抄录，不补字。
以下是合成JSON格式示例。示例ID和文字仅用于展示格式。
{json.dumps(example, ensure_ascii=False)}
实际行ID按以下顺序完整返回，每个恰好一次。每项只含id和text，text为单行字符串或null。输出根对象只含lines。
{json.dumps(ids, ensure_ascii=False)}"""


def validate_transcription(payload, line_ids):
    """Check the fixed physical-row contract and preserve literal output."""
    ids = _expected_ids(line_ids)
    if not isinstance(payload, dict) or payload.keys() != {"lines"}:
        raise LayoutContextError("INVALID_TRANSCRIPTION_ROOT", "transcription")
    lines = payload["lines"]
    if not isinstance(lines, list) or len(lines) != len(ids):
        raise LayoutContextError("TRANSCRIPTION_LINE_COUNT", "transcription.lines")
    result = []
    for index, (line, line_id) in enumerate(zip(lines, ids)):
        location = f"transcription.lines[{index}]"
        if not isinstance(line, dict) or line.keys() != {"id", "text"}:
            raise LayoutContextError("INVALID_TRANSCRIPTION_LINE", location)
        if line["id"] != line_id:
            raise LayoutContextError("TRANSCRIPTION_LINE_ID_ORDER", location+".id")
        text = line["text"]
        if text is not None and (not isinstance(text, str) or not text.strip() or _LINE_BREAKS.intersection(text)):
            raise LayoutContextError("INVALID_TRANSCRIPTION_TEXT", location+".text")
        result.append({"id": line_id, "text": text})
    return {"lines": result}


def _group_transcription(transcription):
    """Only an explicit numbered prefix can establish a paragraph group."""
    if not isinstance(transcription, dict) or not isinstance(transcription.get("lines"), list):
        raise LayoutContextError("INVALID_TRANSCRIPTION_ROOT", "transcription")
    if any(not isinstance(line, dict) for line in transcription["lines"]):
        raise LayoutContextError("INVALID_TRANSCRIPTION_LINE", "transcription.lines")
    ids = [line.get("id") for line in transcription["lines"]]
    lines = validate_transcription(transcription, ids)["lines"]
    paragraph_id = None
    for line in lines:
        text = line["text"]
        if text is None:
            paragraph_id = None
        elif _NUMBERED_PARAGRAPH.match(text):
            paragraph_id = f"p-{line['id']}"
        elif _AMBIGUOUS_PARAGRAPH.match(text):
            paragraph_id = None
        line["paragraph_id"] = paragraph_id
    return lines


def build_relations_prompt(transcription):
    lines = _group_transcription(transcription)
    example = _region_prompt_example("notes")
    demo_line = example["lines"][0]
    demo_measurement = example["measurements"][0]
    for field in demo_measurement.values():
        if isinstance(field, dict):
            field["line_id"] = "demo-line"
    operators = "、".join(sorted(COMPARATORS))
    return f"""从固定逐行转录提取测量关系。返回对象只含measurements数组。无法确定的关系可以保留未绑定字段，缺少可引用测量时返回空数组。
下面是合成示例。假定demo-line的原文为「{demo_line['text']}」。示例文字和编号只用于展示格式。
{json.dumps({'measurements': [demo_measurement]}, ensure_ascii=False, indent=2)}

literal必须是含line_id和quote的原文引用对象。value、unit、comparator、property、object、unit_relation同样使用引用对象，缺证据时把整个字段填null。引用对象的quote必须是实际行里的非空连续原文，不添加、改写或补全词语。用较长literal区分重复数字，value必须在literal内。属性或对象同一行出现多次时，优先选择在该literal范围内恰好出现一次的原文字面。否则只引用原行里唯一可定位的原文，无唯一出处填null。
comparator只能逐字引用数值前紧邻的完整符号，允许的符号为{operators}。缺少可见符号时整个comparator填null，不能把陈述句或普通要求词填入比较符，也不能用quote为null的引用对象。
relation_basis只使用same_phrase或unresolved。只有同一实际行或同一非空paragraph_id内的引文才可共享主体。未读行的text为null，引用不可跨过其不确定范围。继承单位须指向实际单位声明，无声明时unit_origin填unknown、unit填null。尺寸组合保留在literal，无法确认为单个数值时value填null。
conditions是可选的条件引用数组。仅引用本测量所在的原行或已编号段落内适用的条件原文，不得与literal或其他条件引用重叠。跨行条件逐行引用，保留原样。没有可靠条件引用则填[]，不要把比较符或条件补写进去。
输出只含测量引用和关系。行文本、段落ID及位置由程序绑定。以下是实际转录输入。
{json.dumps(lines, ensure_ascii=False, indent=2)}"""


def _bind_relations(payload, transcription, bands, region, source_boundary_status):
    if not isinstance(payload, dict) or payload.keys() != {"measurements"}:
        raise LayoutContextError("INVALID_RELATIONS_ROOT", "relations")
    if not isinstance(payload["measurements"], list):
        raise LayoutContextError("EXPECTED_ARRAY", "relations.measurements")
    if not isinstance(region, dict) or region.get("region_type") != "notes":
        raise LayoutContextError("NOTES_REGION_REQUIRED", "region")
    if not isinstance(bands, dict) or not isinstance(bands.get("lines"), list) or any(
            not isinstance(line, dict) for line in bands["lines"]):
        raise LayoutContextError("INVALID_FIXED_LINE_IDS", "bands.lines")
    frozen = validate_transcription(transcription, [line.get("id") for line in bands["lines"]])
    lines = _group_transcription(frozen)
    unread = [line["id"] for line in lines if line["text"] is None]
    readable = [line for line in lines if line["text"] is not None]
    # Keep missing text explicit without creating invented text for its row.
    # The frozen band IDs are used unchanged for all readable line references.
    available_bands = {**bands, "lines": [line for line in bands["lines"] if line["id"] not in unread]}
    # Reading every detected row does not establish that the parent crop is complete.
    boundary = source_boundary_status if source_boundary_status is not None else region.get("boundary_status")
    context_status = ("partial" if unread or boundary == "partial" else
                      "complete" if boundary == "complete" else "unknown")
    bound = bind_note_lines({"lines": readable, "measurements": payload["measurements"],
                             "context_status": context_status,
                             "table": None, "chain": None}, available_bands)
    return bound, unread


def validate_relations(payload, transcription, bands, region, image_size, *, source_boundary_status=None):
    """Bind model relationships to frozen text and original host-owned boxes."""
    bound, unread = _bind_relations(payload, transcription, bands, region, source_boundary_status)
    result = validate_region(bound, region, image_size)
    result["unread_line_ids"] = unread
    lines = {line["id"]: line for line in result["lines"]}
    paragraphs = {}
    for line in lines.values():
        if line["paragraph_id"] is not None:
            paragraphs.setdefault(line["paragraph_id"], []).append(line["id"])
    result["paragraphs"] = [{"id": key, "line_ids": ids} for key, ids in paragraphs.items()]
    for measurement in result["measurements"]:
        line = lines[measurement["literal"]["line_id"]]
        paragraph = line["paragraph_id"]
        measurement["context_line_ids"] = paragraphs[paragraph] if paragraph else [line["id"]]
        measurement.setdefault("conditions", [])
        measurement["condition_status"] = "candidate" if measurement["conditions"] else "unresolved"
    return result


def diagnose_relations(payload, transcription, bands, region, image_size):
    """Return safe error codes and positions without generating replacement readings."""
    try:
        bound, _ = _bind_relations(payload, transcription, bands, region, None)
        evidence = _RegionEvidence(region, image_size, bound)
    except LayoutContextError as error:
        return [{"code": error.code, "location": error.location}]
    diagnostics, measurement_ids = [], set()
    for index, raw in enumerate(bound["measurements"]):
        path = f"measurements[{index}]"
        errors = []
        try:
            reading = _measurement(raw, path, evidence, None, None, field_errors=errors)
            if reading:
                if reading["id"] in measurement_ids:
                    raise LayoutContextError("DUPLICATE_ID", path)
                measurement_ids.add(reading["id"])
        except LayoutContextError as error:
            errors.append(error)
        diagnostics.extend({"code": error.code, "location": error.location} for error in errors)
    return diagnostics
