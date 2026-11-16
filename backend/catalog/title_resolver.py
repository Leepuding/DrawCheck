"""Resolve a title number from positioned CAD text without correcting characters.

Only a labelled, unique field intersecting the supplied title area can replace
the visual reading. Filenames, expected catalogues and string similarity are
deliberately absent from this boundary.
"""
import math
import re

from backend.store import Source
from backend.title_identity import has_manual_number, manual_number_mode


_DRAWING_LABELS = {"图号", "图纸编号", "图纸号", "drawingno", "drawingnumber", "dwgno", "dwgnumber"}
_OTHER_LABELS = {"版号", "版次", "版本", "日期", "比例", "图名", "图纸名称", "设计", "制图", "校核", "审核", "批准",
                 "专业", "页次", "项目", "项目号", "图幅", "revision", "rev", "date", "scale", "sheet", "sheetno", "sheetnumber", "drawingtitle"}
_INLINE = re.compile(r"^\s*(图\s*号|图纸编号|图纸号|drawing\s*(?:no\.?|number)|dwg\s*(?:no\.?|number))\s*[:：]\s*(.+?)\s*$", re.I)


def _label(text):
    return re.sub(r"[\s.：:]", "", text).lower()


def _box(row):
    if not isinstance(row, dict):
        return None
    try:
        values = tuple(row[key] for key in ("x0", "y0", "x1", "y1"))
        if any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or not 0 <= v <= 1 for v in values):
            return None
        return values if values[2] > values[0] and values[3] > values[1] else None
    except (KeyError, TypeError):
        return None


def _intersects(a, b):
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def _same_row(a, b):
    ah, bh = a[3]-a[1], b[3]-b[1]
    overlap = min(a[3], b[3])-max(a[1], b[1])
    return overlap >= min(ah, bh)/2 and abs((a[1]+a[3]-b[1]-b[3])/2) <= max(ah, bh)/2


def _identifier(text):
    # Syntax is intentionally broad: no project prefix, sheet count or common
    # S/5/I/1 confusion mapping. Preserve every character of the selected value.
    return (bool(text) and any(ch.isascii() and ch.isalnum() for ch in text)
            and all(ch.isalnum() or ch in "-_./\\()—–－" for ch in text)
            and not any(ch.isspace() for ch in text))


def _evidence(fact):
    return {"fact_id": fact["id"], "source": fact["source"], "text": fact["text"],
            "bbox": {key: fact[key] for key in ("x0", "y0", "x1", "y1")}}


def resolve_title_number(unit, block, facts):
    vision = str(block.get("drawing_number") or "")
    result = {"status": "not_applicable", "vision_drawing_number": vision,
              "selected_drawing_number": vision, "selected_source": Source.VISION.value,
              "selected_evidence": None, "label_evidence": None, "candidate_evidence": [],
              "rejected_evidence": [],
              "differs_from_vision": False, "requires_confirmation": False}
    if has_manual_number(unit):
        result.update(status="manual", selected_drawing_number=unit.get("drawing_number") or "",
                      selected_source=Source.USER.value, manual_confirmation_mode=manual_number_mode(unit))
        previous = (unit.get("metadata") or {}).get("title_number_resolution") or {}
        prior = previous.get("selected_evidence") if previous.get("selected_source") == Source.CAD_TEXT.value else previous.get("prior_cad_evidence")
        if prior:
            result["prior_cad_evidence"] = prior
        return result
    if unit.get("kind") != "dwg_frame":
        return result
    cad = [f for f in facts if f.get("unit_id") == unit["id"] and f.get("source") == Source.CAD_TEXT.value
           and isinstance(f.get("text"), str) and _box(f) is not None]
    if not cad:
        return result
    # Catalogue volume IDs have their own labelled header field. They do not
    # become ordinary sheet numbers, and do not depend on a model title bbox.
    from backend.extract.local_sheet import Word, catalog_identity_field
    reading = catalog_identity_field([Word(f["text"], _box(f), 1., f["source"]) for f in cad])
    if reading:
        def original(evidence):
            return next((f for f in cad if f["text"] == evidence["text"]
                         and _box(f) == tuple(evidence["bbox"][key] for key in ("x0", "y0", "x1", "y1"))), None)
        value, label, heading = (original(reading[key]) for key in ("value", "label", "heading"))
        if value and label and heading:
            result.update(status="cad_selected", identity_kind="catalog_volume",
                selected_drawing_number=reading["number"], selected_source=Source.CAD_TEXT.value,
                selected_evidence=_evidence(value), label_evidence=_evidence(label),
                heading_evidence=_evidence(heading), candidate_evidence=[_evidence(value)],
                differs_from_vision=reading["number"] != vision, requires_confirmation=False,
                reason="目录原文中唯一卷册检索字段；卷册身份与单张图号分别记录")
            return result
    labels = [f for f in cad if _label(f["text"]) in _DRAWING_LABELS]
    inline = [(f, match.group(2).strip()) for f in cad if (match := _INLINE.fullmatch(f["text"]))]
    if not labels and not inline:
        result.update(status="no_label", reason="未找到可定位的 CAD 图号标签，保留视觉流程")
        return result
    title_box = _box(block.get("bbox"))
    if title_box is None or block.get("bbox_clamped") or block.get("no_title_block"):
        result.update(status="position_unverified", requires_confirmation=True,
                      reason="CAD 图号标签缺少可核实的图签区域，需确认")
        return result
    boundaries = [f for f in cad if _label(f["text"]) in _OTHER_LABELS | _DRAWING_LABELS]
    candidates = []
    outside = False
    for label in labels:
        box = _box(label)
        right = min([_box(f)[0] for f in boundaries if f["id"] != label["id"]
                     and _box(f)[0] >= box[2] and _same_row(box, _box(f))], default=1.)
        # Limit the field in drawing units derived from the label's own size,
        # so unrelated same-row notes cannot be reached across the whole sheet.
        right = min(right, box[2] + max(6*(box[2]-box[0]), 8*(box[3]-box[1])))
        for fact in cad:
            value = fact["text"].strip()
            candidate_box = _box(fact)
            if (fact["id"] == label["id"] or _label(value) in _OTHER_LABELS | _DRAWING_LABELS
                    or not _identifier(value) or candidate_box[0] < box[2]
                    or candidate_box[2] > right or not _same_row(box, candidate_box)):
                continue
            if not (_intersects(box, title_box) and _intersects(candidate_box, title_box)):
                outside = True
                result["rejected_evidence"].append({"evidence":_evidence(fact), "label_evidence":_evidence(label),
                                                    "reason":"outside_title_bbox"})
                continue
            candidates.append((fact, label, value))
    for fact, value in inline:
        if not _identifier(value):
            continue
        if _intersects(_box(fact), title_box):
            candidates.append((fact, fact, value))
        else:
            outside = True
            result["rejected_evidence"].append({"evidence":_evidence(fact), "label_evidence":_evidence(fact),
                                                "reason":"outside_title_bbox"})
    # Duplicate references to the same fact do not create ambiguity; different
    # facts (even with identical strings) retain their distinct evidence anchors.
    candidates = list({(fact["id"], value): (fact, label, value) for fact, label, value in candidates}.values())
    result["candidate_evidence"] = [_evidence(fact) for fact, _, _ in candidates]
    if len(candidates) != 1:
        result.update(status="ambiguous" if len(candidates)>1 else "position_unverified" if outside else "field_unverified",
                      requires_confirmation=True, reason="CAD 图号字段候选不唯一或位置未核实，需确认")
        return result
    fact, label, value = candidates[0]
    result.update(status="cad_selected", selected_drawing_number=value, selected_source=Source.CAD_TEXT.value,
                  selected_evidence=_evidence(fact), label_evidence=_evidence(label),
                  differs_from_vision=value != vision, reason="图签区域内唯一且有图号标签的 CAD 原文")
    return result
