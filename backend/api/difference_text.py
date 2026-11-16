"""Describe the two readings separately from the evidence for pairing them."""
import re
from decimal import Decimal, InvalidOperation
from itertools import zip_longest

from backend.title_identity import project_code

NO_DIFFERENCE = "规则未记录具体差异"
NO_BASIS = "规则未记录对照依据"


def _number(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _catalog_segments(left, right):
    pattern = r"[A-Za-z0-9]+(?:[-_./][A-Za-z0-9]+)+"
    if not all(re.fullmatch(pattern, code) and any(c.isdigit() for c in code)
               for code in (left, right)):
        return ""
    a, b = (re.split(r"([-_./])", code) for code in (left, right))
    # A delimiter change is a literal difference as well.
    changes, equal = [], 0
    for i, (x, y) in enumerate(zip_longest(a, b)):
        if x == y:
            equal += i % 2 == 0
        else:
            label = f"第 {i // 2 + 1} 段" if not i % 2 else f"第 {i // 2 + 1} 个分隔符"
            changes.append(f"{label} {x or '（无）'} ↔ {y or '（无）'}")
    if equal:
        changes.append(f"其余 {equal} 段一致" if len(a) == len(b) else f"{equal} 段一致")
    return "；" + "，".join(changes) if changes else ""


def describe_difference(category, rule, basis):
    """Describe recorded values only; never infer which side is correct."""
    left, right = (str(rule.get(key) or "").strip() for key in ("left_value", "right_value"))
    fixture = rule.get("fixture_id")
    reason = None
    if category in {"目录与图签", "目录与图签不符"}:
        if fixture == "ISSUE-CAT-MISSING" and left:
            reason = f"目录列有「{left}」，已核实的实到图中没有这张图；请责任方核对是否需补图。"
        elif fixture == "ISSUE-CAT-EXTRA" and left:
            reason = f"实到图签写「{left}」，已核实的目录未列这张图；请责任方核对是否需补列。"
        elif left and right and left != right:
            reason = f"目录写「{left}」，图签写「{right}」{_catalog_segments(left, right)}。"
            if (a := project_code(left)) and (b := project_code(right)) and a != b:
                reason += "项目代码段不同，可能沿用了其他项目的图框。"
            reason += "请图纸责任方确认应采用的图号。"
    elif category == "目录张数":
        a, b = _number(left), _number(right)
        if a is not None and b is not None and a != b:
            reason = f"目录列 {left} 张，实到 {right} 张，相差 {abs(a-b)} 张；请责任方核对清单。"
    elif category in {"尺寸", "标高"}:
        dim = category == "尺寸"
        diff = _number(rule.get("max_abs_diff" if dim else "abs_diff", rule.get("max_abs_diff")))
        unit = "mm" if dim else "m"
        tolerance = _number(rule.get("tolerance_mm" if dim else "tolerance_m"))
        if diff is not None and diff > 0:
            limits = f"，允许偏差 {tolerance:g} {unit}" if tolerance is not None else ""
            reason = f"两边标注相差 {diff} {unit}{limits}。请责任方核对应采用的{category}。"
        elif left and right and left != right:
            components = rule.get("component_counts")
            if dim and components and components[0] != components[1]:
                reason = f"图纸一标 {components[0]} 个尺寸分量，图纸二标 {components[1]} 个。"
            else:
                reason = f"图纸一{category}标为「{left}」，图纸二标为「{right}」。"
            reason += f"请责任方核对应采用的{category}。"
    elif category == "详图索引断链" and fixture == "ISSUE-INDEX-BROKEN" and left:
        target = rule.get("reference_target")
        detail = rule.get("reference_detail")
        if target and detail:
            reason = f"已找到图号「{target}」，核实的详图清单没有「{detail}」。请责任方核对索引。"
        elif target:
            reason = f"原文「{left}」所指的「{target}」未在实到图中找到。请责任方核对索引。"
        else:
            reason = f"原文「{left}」没有可用的对应图纸或详图。请责任方核对索引。"
    elif left and right and left != right and right != "（不存在）":
        if category == "编号写法":
            readings = rule.get("compared_number_literals") or {}
            left, right = readings.get("left") or left, readings.get("right") or right
        field = {"材料规格": "规格", "数量与材料表": "数量或计量值", "编号写法": "编号",
                 "接线": "接线标注", "电缆编号": "电缆编号", "设备位号": "设备位号"}.get(category)
        if category == "图签编号前缀":
            reason = f"图签写「{left}」，约定前缀为「{right}」；请责任方核对编号。"
        elif field and left != right:
            delta = _number(rule.get("quantity_difference")) if category == "数量与材料表" else None
            count = f"，相差 {delta} {rule['quantity_unit']}" if delta and rule.get("quantity_unit") else ""
            reason = f"图纸一{field}写「{left}」，图纸二写「{right}」{count}；请责任方核对采用哪一处。"
    return reason if reason and reason != basis else NO_DIFFERENCE


def comparison_basis(category, rule):
    """Prefer the pairing context of known rules, including historical rows."""
    method = rule.get("match_basis")
    fixture = rule.get("fixture_id") or ""
    if method == "unique_literal_name_in_source":
        return "同一源文件中，按目录与图签唯一对应的图名配对。"
    if method == "dwg_shared_block_mark":
        return "按两张图共用底图的同一标注位置配对。"
    if method == "local_engineering_identity":
        return "按同一对象、标注位置及适用基准配对。"
    if category in {"尺寸", "标高"} and (fixture.startswith(("ISSUE-DIM-", "ISSUE-ELV-"))
                                        or rule.get("object_label")):
        label = rule.get("object_label") or category
        return f"按已确认的同一对象或标注项目「{label}」配对。"
    if fixture in {"ISSUE-SPEC", "ISSUE-NUM-STYLE", "ISSUE-QTY"} and not method:
        return "按已核实的同一对象和标注项目配对。"
    if fixture in {"ISSUE-CAT-MISSING", "ISSUE-CAT-EXTRA", "ISSUE-CAT-COUNT"}:
        return "将完整可读目录与已核实的实到图签清单对照。"
    if fixture == "ISSUE-INDEX-BROKEN":
        return "按引用原文核对实到图号和适用的详图清单。"
    if fixture == "ISSUE-PREFIX-BAD":
        return "按图签原文核对项目约定的编号前缀。"
    if method in {"cad_signal_code", "cad_cable_range_field", "cad_circuit_endpoint_geometry"}:
        return {"cad_signal_code": "按唯一的来源及目的信号箭头的相同信号码配对。",
                "cad_cable_range_field": "按同柜电缆编号字段配对。",
                "cad_circuit_endpoint_geometry": "按同一线路编号及两侧明确列全的端点配对。"}[method]
    value = str(rule.get("basis") or "").strip()
    return value if value and value != NO_DIFFERENCE else NO_BASIS
