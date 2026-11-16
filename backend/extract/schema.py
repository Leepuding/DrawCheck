"""结构化识别结果校验。"""
from __future__ import annotations

import json
import math
from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import CODE_BAD_FORMAT, ExtractError


def parse_model_json(content: str) -> dict[str, Any]:
    """解析模型返回为 dict；失败抛 EXTRACT_BAD_FORMAT。"""
    text = (content or "").strip()
    if not text:
        raise ExtractError(CODE_BAD_FORMAT, "模型返回为空")
    # GLM vision final answers may contain this exact, balanced output envelope.
    # Strip only the enclosing protocol markers, never search arbitrary prose/reasoning for JSON.
    start, end = "<|begin_of_box|>", "<|end_of_box|>"
    if text.startswith(start) and text.endswith(end):
        text = text[len(start):-len(end)].strip()
    # 容忍偶发 Markdown 围栏
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExtractError(CODE_BAD_FORMAT, f"模型返回非 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ExtractError(CODE_BAD_FORMAT, "模型返回根须为对象")
    return data


def validate_extract_payload(data: dict[str, Any]) -> dict[str, Any]:
    """校验并规范化 title_blocks / facts；不合格抛 EXTRACT_BAD_FORMAT。"""
    if not isinstance(data, dict):
        raise ExtractError(CODE_BAD_FORMAT, "识别结果须为对象")
    if "title_blocks" not in data and "facts" not in data:
        raise ExtractError(CODE_BAD_FORMAT, "缺少 title_blocks 与 facts")

    title_blocks = data.get("title_blocks")
    facts = data.get("facts")
    if title_blocks is None:
        title_blocks = []
    if facts is None:
        facts = []
    if not isinstance(title_blocks, list) or not isinstance(facts, list):
        raise ExtractError(CODE_BAD_FORMAT, "title_blocks/facts 须为数组")

    norm_titles = [_norm_title(item, i) for i, item in enumerate(title_blocks)]
    norm_facts = [fact for i, item in enumerate(facts) for fact in _norm_fact_locations(item, i)]
    return {
        "prompt_version": data.get("prompt_version"),
        "title_blocks": norm_titles,
        "facts": norm_facts,
        **_norm_catalog(data),
    }



def _norm_catalog(data):
    # Old replay/model payloads have no directory fields: leave existing metadata alone.
    if "is_catalog_sheet" not in data:
        return {}
    if type(data["is_catalog_sheet"]) is not bool:
        raise ExtractError(CODE_BAD_FORMAT, "is_catalog_sheet 须为布尔值")
    result = {"is_catalog_sheet":data["is_catalog_sheet"]}
    if data.get("catalog_volume_numbers_uncertain") is True:
        result["catalog_volume_numbers_uncertain"] = True
    if not data["is_catalog_sheet"]:
        return result
    for key in ("catalog_listed_numbers", "catalog_volume_numbers"):
        if key not in data:
            continue
        value = data[key]
        if key == "catalog_volume_numbers":
            # A provider may return a numeric volume ordinal (e.g. [1]) here.
            # It is not a verbatim drawing identifier: never stringify it or use
            # it to remove a listed sheet. Keep the independently read table rows.
            if not isinstance(value, list):
                result["catalog_volume_numbers_uncertain"] = True
                value = []
            elif any(not isinstance(item, str) for item in value):
                result["catalog_volume_numbers_uncertain"] = True
                value = [item for item in value if isinstance(item, str)]
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ExtractError(CODE_BAD_FORMAT, f"{key} 须为图号原文字符串数组")
        result[key] = list(dict.fromkeys(item.strip() for item in value if item.strip()))
    result["catalog_complete"] = data.get("catalog_complete") is True
    if "catalog_bbox" in data:
        result["catalog_bbox"] = data["catalog_bbox"]
    if "catalog_entries" in data:
        entries = data["catalog_entries"]
        if not isinstance(entries, list):
            raise ExtractError(CODE_BAD_FORMAT, "catalog_entries 须为数组")
        result["catalog_entries"] = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("drawing_number"), str):
                raise ExtractError(CODE_BAD_FORMAT, "catalog_entries 须提供原文图号")
            result["catalog_entries"].append({"drawing_number":entry["drawing_number"].strip(),
                "bbox":entry.get("bbox"), "confidence":_as_confidence(entry.get("confidence"), 0, "catalog"),
                "unclear":bool(entry.get("unclear", False)), **_retained_evidence_flags(entry)})
    volumes = set(result.get("catalog_volume_numbers") or [])
    if "catalog_listed_numbers" in result:
        result["catalog_listed_numbers"] = [number for number in result["catalog_listed_numbers"] if number not in volumes]
    return result

def _norm_title(item: Any, index: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ExtractError(CODE_BAD_FORMAT, f"title_blocks[{index}] 须为对象")
    conf = _as_confidence(item.get("confidence"), index, "title")
    return {
        "specialty": str(item.get("specialty") or ""),
        "drawing_number": str(item.get("drawing_number") or ""),
        "name": str(item.get("name") or ""),
        "bbox": item.get("bbox"),
        "confidence": conf,
        "unclear": bool(item.get("unclear", False)),
        "no_title_block": bool(item.get("no_title_block", False)),
        "multi_frame": bool(item.get("multi_frame", False)),
        "out_of_scope": bool(item.get("out_of_scope", False)),
        "unlisted_in_catalog_sheet": bool(item.get("unlisted_in_catalog_sheet", False)),
        "file": item.get("file"),
        "page": item.get("page"),
        "page_size": item.get("page_size"),
        **_retained_evidence_flags(item),
    }



def normalize_numeric_fact(item: dict[str, Any]) -> dict[str, Any]:
    """A store numeric value is one finite decimal, never an interval or an array.

    Preserve visible text as the authority. Even a singleton list may describe
    a count/specification collection, so silently taking its first item is unsafe.
    """
    row = dict(item)
    if row.get("unit") is not None and not isinstance(row["unit"], str):
        # A unit array/object/ordinal is not a readable unit. Never guess or
        # stringify it, including when replaying pre-validation cached facts.
        row.update(unit=None, unit_invalid=True, unclear=True)
    raw = row.get("numeric_value")
    if raw is None:
        return row
    valid = isinstance(raw, (str, int, float, Decimal)) and not isinstance(raw, bool)
    value = str(raw).strip() if valid else ""
    try:
        valid = valid and bool(value) and Decimal(value).is_finite()
    except (InvalidOperation, ValueError):
        valid = False
    if valid:
        row["numeric_value"] = value
    else:
        row.update(numeric_value=None, numeric_value_invalid=True, unclear=True)
    return row

def _norm_fact_locations(item: Any, index: int) -> list[dict[str, Any]]:
    """Keep explicitly repeated physical locations as separate evidence.

    A list of boxes is not a missing box or a single large union. Values,
    categories, uncertainty and object identity remain exactly as supplied;
    locations do not assign individual values from a numeric array.
    """
    if not isinstance(item, dict) or not isinstance(item.get("bbox"), list):
        return [_norm_fact(item, index)]
    boxes = item["bbox"]
    if not 1 <= len(boxes) <= 16:
        raise ExtractError(CODE_BAD_FORMAT, f"facts[{index}].bbox 位置数须为 1–16")
    result, seen = [], set()
    keys = ("x0", "y0", "x1", "y1")
    for box in boxes:
        if not isinstance(box, dict) or set(box) != set(keys):
            raise ExtractError(CODE_BAD_FORMAT, f"facts[{index}].bbox 多位置须为完整坐标对象")
        values = tuple(box[key] for key in keys)
        try:
            finite = all(not isinstance(value, bool) and isinstance(value, (int, float))
                         and math.isfinite(value) for value in values)
        except OverflowError:
            finite = False
        if not finite:
            raise ExtractError(CODE_BAD_FORMAT, f"facts[{index}].bbox 多位置须为有限数值")
        x0, y0, x1, y1 = values
        if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
            raise ExtractError(CODE_BAD_FORMAT, f"facts[{index}].bbox 多位置须为有效归一化框")
        if values not in seen:
            seen.add(values)
            result.append(_norm_fact({**item, "bbox": dict(box)}, index))
    return result


def _norm_fact(item: Any, index: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ExtractError(CODE_BAD_FORMAT, f"facts[{index}] 须为对象")
    text = item.get("text")
    if text is None:
        raise ExtractError(CODE_BAD_FORMAT, f"facts[{index}] 缺 text")
    conf = _as_confidence(item.get("confidence"), index, "fact")
    return normalize_numeric_fact({
        "category": str(item.get("category") or ""),
        "text": str(text),
        "numeric_value": item.get("numeric_value"),
        "unit": item.get("unit"),
        "bbox": item.get("bbox"),
        "confidence": conf,
        "unclear": bool(item.get("unclear", False)),
        "source": item.get("source") or "看图识别",
        **{key: str(item[key]) for key in ("object_id", "comparison_key", "target_drawing_number", "target_detail_number", "detail_number", "quantity_role") if item.get(key) is not None},
        "identity_confirmed": item.get("identity_confirmed") is True,
        **_retained_evidence_flags(item),
    })


def _retained_evidence_flags(item: dict[str, Any]) -> dict[str, Any]:
    """Preserve conservative tile/cache annotations through revalidation."""
    result = {key: True for key in ("region_conflict", "bbox_clamped", "region_bbox_unlocated",
                                   "numeric_value_invalid", "unit_invalid") if item.get(key) is True}
    regions = item.get("region_ids")
    if isinstance(regions, list) and all(isinstance(value, str) for value in regions):
        result["region_ids"] = list(dict.fromkeys(regions))
    for key in ("model_original_bbox","model_original_flags","position_recovery"):
        if isinstance(item.get(key),dict):
            # Audit only; ExtractService independently rebuilds/validates the
            # active position against the current immutable image on replay.
            result[key]=item[key]
    if any(result.get(key) for key in ("region_conflict", "bbox_clamped", "numeric_value_invalid", "unit_invalid")):
        result["unclear"] = True
    return result


def _as_confidence(value: Any, index: int, kind: str) -> float:
    if value is None:
        return 1.0
    if isinstance(value, bool):
        raise ExtractError(CODE_BAD_FORMAT, f"{kind}[{index}].confidence 非法")
    try:
        conf = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ExtractError(CODE_BAD_FORMAT, f"{kind}[{index}].confidence 非法") from exc
    if not math.isfinite(conf) or conf < 0 or conf > 1:
        raise ExtractError(CODE_BAD_FORMAT, f"{kind}[{index}].confidence 须在 0～1")
    return conf
