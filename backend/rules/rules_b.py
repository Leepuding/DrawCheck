"""规则 B：仅比较有对象身份依据的事实；所有数量计算在本地完成。"""
from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import combinations
from typing import Any

from backend.extract.semantic import _COUNT
from backend.store import Evidence, Source
from backend.references import (SAME_VOLUME_SHORTHAND, drawing_reference, resolve_reference, absence_scope,
                                import_scope_diagnostics, verified_sheet_number, volume_reference, visible_reference_hint)
from .common import compared_object, comparison_row, evidence_from_fact

UNPAIRED_REASON = "未确认对应同一对象，无法比较，待确认"
INVALID_NUMBER_REASON = "编号原文包含多个编号、范围、材料/尺寸表达或无唯一明确编号，待确认"
NO_REFERENCE_REASON = "缺少可核实的引用关系原文，待确认"
OPEN_SCOPE_REASON = "引用目标缺失尚不能成立：识别身份或目录范围未闭合，待确认"
CATEGORIES = {"编号写法", "材料规格", "数量与材料表", "详图索引断链"}
_CODE = re.compile(r"^([A-Za-z]+)-?(\d+)([A-Za-z]*)$")
_COUNT_LABEL = re.compile(r"^(?:材料表数量|图上计数|数量|计数)\s*[:：]?\s*")
_BARE_COUNT = re.compile(r"\d+(?:\.\d+)?")
_TABLE_ROW_ID = re.compile(
    r"(?:(?:表内)?(?:构件|部件|材料|物料)?(?:编号|序号|行号)[:#-]?\d+|第\d+行)"
    r"(?:[-_/](?:名称|数量|单位|规格|型号|材质|备注|编号|序号))?"
)


def category(fact):
    if volume_reference(fact.get("text")) and drawing_reference(fact) is None:
        return None
    if drawing_reference(fact) or visible_detail_hint(fact):
        return "详图索引断链"
    explicit = fact.get("category")
    aliases = {"编号": "编号写法", "规格": "材料规格", "数量": "数量与材料表", "详图索引": "详图索引断链"}
    if explicit in CATEGORIES or explicit in aliases:
        selected = aliases.get(explicit, explicit)
        return None if selected == "详图索引断链" else selected
    text = fact.get("text", "").strip()
    if re.match(r"(?:数量|图上计数|材料表数量)\s*\d", text):
        return "数量与材料表"
    if re.search(r"(?:YJV|TMY|DN\d|规格)", text, re.I):
        return "材料规格"
    if _CODE.fullmatch(text):
        return "编号写法"
    return None


def annotated_facts(store, version_id):
    from backend.evidence_selection import selected_facts
    return selected_facts(store, version_id)


def _canonical_code(text):
    match = _CODE.fullmatch(text.strip())
    return (match[1].upper(), int(match[2]), match[3].upper()) if match else None


_NUMBER_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*")


def _visible_number(fact):
    """Read one literal identifier, never the whole explanatory label.

    Material/dimension expressions and lists/ranges do not establish a unique
    object number. No joining, case folding, zero stripping or OCR correction.
    """
    text = fact.get("text") or ""
    result = {"fact_id":fact.get("id"), "raw_text":text, "literal":None,
              "reason":None, "candidates":[]}
    tokens = _NUMBER_TOKEN.findall(text)
    candidates = [token for token in tokens if re.search(r"[A-Za-z]", token) and re.search(r"\d", token)]
    result["candidates"] = candidates
    material = (fact.get("unit") or re.search(r"材质|材料|规格|型钢|钢筋|混凝土|电缆|导线|管径|壁厚", text)
                or re.search(r"(?:YJV|TMY|DN\d|HRB\d|HPB\d|Q\d{3}[A-Za-z]?)", text, re.I)
                or re.search(r"\d\s*[×xX*]\s*\d", text))
    ranged = (re.search(r"\d\s*(?:[~～—–]|至|到)\s*[A-Za-z]*\d", text)
              or re.search(r"\d\s*-\s*\d", text)
              or re.search(r"([A-Za-z]+)\d+\s*-\s*\1\d+", text))
    if material or ranged or len(candidates) != 1 or any(token.isdigit() for token in tokens):
        result["reason"] = INVALID_NUMBER_REASON
    else:
        result["literal"] = candidates[0]
    return result


def _specific_identity(value):
    """A generic name/attribute or table-local serial is not an object identity."""
    if not isinstance(value, str) or not value.strip():
        return False
    value = value.strip()
    if re.fullmatch(r"\d+", value) or re.match(r"序号\s*\d+", value) or re.fullmatch(r"\d+[_ /-].*表", value):
        return False
    return (bool(re.search(r"[A-Za-z\u4e00-\u9fff].*\d|\d.*[A-Za-z\u4e00-\u9fff]", value))
            or bool(re.fullmatch(r"[A-Za-z]+(?:[-_][A-Za-z]+)+", value)))


def visible_detail_hint(fact):
    return visible_reference_hint(fact)


def _quantity_table_row_id(fact):
    # Older cached table rows may lack quantity_role. A local serial remains
    # local even when the model marked the same serial on two sheets confirmed.
    return category(fact) == "数量与材料表" and any(
        isinstance(value, str) and _TABLE_ROW_ID.fullmatch("".join(unicodedata.normalize("NFKC", value).split()))
        for value in (fact.get("object_id"), fact.get("comparison_key"))
    )


def object_review_candidates(facts):
    """Ask about identity only where two distinct drawings offer a plausible comparison."""
    groups = defaultdict(list)
    for fact in facts:
        key = fact.get("object_id") or fact.get("comparison_key")
        if _specific_identity(key) and not _quantity_table_row_id(fact):
            groups[key, fact.get("comparison_key")].append(fact)
    return {f["id"] for group in groups.values()
            if len({f["unit_id"] for f in group}) > 1 and len({f["text"].strip() for f in group}) > 1
            for f in group}


def _same_object(a, b):
    if _quantity_table_row_id(a) or _quantity_table_row_id(b):
        return False
    # Table-local row serials/names do not corroborate other rows in that table.
    if (a.get("unit_id") == b.get("unit_id")
            and a.get("quantity_role") == b.get("quantity_role") == "material_table"):
        return False
    if a.get("object_id") and b.get("object_id"):
        return (a["object_id"] == b["object_id"] and _specific_identity(a["object_id"])
                and bool(a.get("identity_confirmed") and b.get("identity_confirmed")))
    if a.get("comparison_key") and a.get("comparison_key") == b.get("comparison_key"):
        return (_specific_identity(a["comparison_key"])
                and bool(a.get("identity_confirmed") and b.get("identity_confirmed")))
    if category(a) == "编号写法":
        return _canonical_code(a["text"]) is not None and _canonical_code(a["text"]) == _canonical_code(b["text"])
    return False


def _uncertain(f):
    from backend.evidence_selection import reliable_fact
    return not reliable_fact(f)


def _quantity_reading(fact):
    """Return (value, unit), distinguishing a bare count from an unknown unit."""
    text = _COUNT_LABEL.sub("", fact["text"].strip())
    unit = (fact.get("unit") or "").strip()
    unit = "m" if unit == "米" else unit
    if unit:
        text = re.sub(r"^长度\s*[:：]?\s*", "", text)
    literal = _COUNT.fullmatch(re.sub(r"(?<=\d)\s+(?=[^\d\s])", "", text))
    if literal:
        visible_unit = "m" if literal["unit"] == "米" else literal["unit"]
        if unit and unit != visible_unit:
            return None, None
        return Decimal(literal["value"]), visible_unit
    if not unit:
        if _BARE_COUNT.fullmatch(text) and fact.get("quantity_role") != "material_table":
            return Decimal(text), ""
        return None, None
    literal = re.fullmatch(rf"(\d+(?:\.\d+)?)\s*(?:{re.escape(unit)})?", text)
    if literal:
        return Decimal(literal[1]), unit
    return None, None


@dataclass
class RulesBResult:
    issues: list[dict[str, Any]] = field(default_factory=list)
    comparisons: list[dict[str, Any]] = field(default_factory=list)
    pendings: list[dict[str, Any]] = field(default_factory=list)
    incomplete: list[dict[str, Any]] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)


class RulesBService:
    def __init__(self, store):
        self.store = store

    def run(self, version_id, *, persist=True, comparison_facts=None, **_kwargs):
        if comparison_facts is None:
            units, facts = annotated_facts(self.store, version_id)
        else:
            facts = comparison_facts
            persist = False
        result = RulesBResult()
        pending_keys = set()
        existing_pending = self.store.list_records("pending_confirmations", version_id=version_id) if comparison_facts is None else []
        existing_issues = self.store.list_records("issues", version_id=version_id) if comparison_facts is None else []
        number_diagnostics = []

        def pending(f, reason):
            if comparison_facts is not None:
                return
            key = (f["id"], reason)
            if key in pending_keys:
                return
            pending_keys.add(key)
            found = next((p for p in existing_pending if p["unit_id"] == f["unit_id"] and p["text"] == f["text"] and p["reason"] == reason and p["x0"] == f["x0"] and p["y0"] == f["y0"]), None)
            result.pendings.append(found or (self.store.create_pending(version_id, evidence_from_fact(f), reason) if persist else {"reason": reason, "text": f["text"]}))

        def issue(a, b, cat, basis, fixture_id=None, compared_literals=None, **fields):
            if comparison_facts is not None:
                return
            identity = sorted([a["id"], b["id"]]) if b else [a["id"]]
            key = "rules_b:" + ":".join(identity) + ":" + cat
            left = evidence_from_fact(a)
            right = evidence_from_fact(b) if b else Evidence(a["unit_id"], Source.VISION, "（不存在）", left.bbox)
            columns = {**left.columns("left_"), **right.columns("right_")}
            # A fact ID or a shared left anchor is not the reviewed pair. Both
            # complete evidence snapshots must match, just as in write_issue.
            # Changed evidence leaves the old manual audit outside active_ids,
            # so reconciliation can request a human recheck instead of silently
            # carrying an exclusion/confirmation onto a different right side.
            prior = next((old for old in existing_issues
                if (old.get("rule_result") or {}).get("category") == cat
                and (old.get("rule_result") or {}).get("left_value") == a["text"]
                and (old.get("rule_result") or {}).get("right_value") == right.text
                and all(old.get(name) == value for name, value in columns.items())), None)
            if prior:
                result.issues.append(prior)
                return
            title = f"{cat}：{a['text']}" + (f" 对 {b['text']}" if b else "，目标不存在")
            rr = {"category": cat, "basis": basis, "summary": title, "left_value": a["text"], "right_value": b["text"] if b else "（不存在）", "dedupe_key": key, "fact_ids": identity,
                  "object_label": compared_object(a, b, cat) if b else cat, **fields}
            if fixture_id:
                rr["fixture_id"] = fixture_id
            if compared_literals is not None:
                rr["compared_number_literals"] = compared_literals
            result.issues.append(self.store.create_issue(version_id, title, left, right, rule_result=rr,
                primary_opinion={"engine": "rules_b"}) if persist else {"title": title, "rule_result": rr})

        for cat in sorted(CATEGORIES - {"详图索引断链"}):
            selected = [f for f in facts if (comparison_facts is None or f.get("identity_confirmed"))
                        and category(f) == cat]
            candidates = object_review_candidates(selected)
            number_reads = {}
            if cat == "编号写法":
                usable = []
                for fact in selected:
                    reading = _visible_number(fact)
                    number_diagnostics.append(reading)
                    if reading["literal"] is None:
                        if fact["id"] in candidates:
                            pending(fact, reading["reason"])
                    else:
                        number_reads[fact["id"]] = reading["literal"]
                        usable.append(fact)
                selected = usable
            paired = set()
            for a, b in combinations(selected, 2):
                # Cross-unit comparison requires explicit same-object proof. Co-location alone is never proof.
                if not _same_object(a, b):
                    continue
                if a.get("comparison_key") and b.get("comparison_key") and a["comparison_key"] != b["comparison_key"]:
                    continue
                paired.update([a["id"], b["id"]])
                record_pair = comparison_facts is not None and a["unit_id"] != b["unit_id"]
                label = compared_object(a, b, cat)
                if _uncertain(a) or _uncertain(b):
                    if record_pair:
                        result.comparisons.append(comparison_row(a, b, category=cat, label=label,
                            outcome="unverified", basis="至少一处标注未能读清"))
                    pending(a if _uncertain(a) else b, "事实看不清或把握度不足，待确认")
                    continue
                compared_literals = None
                if cat == "数量与材料表":
                    av, a_unit = _quantity_reading(a)
                    bv, b_unit = _quantity_reading(b)
                    if a_unit is None or b_unit is None or a_unit != b_unit:
                        reason = "两处数量的计量单位不同或未核实，无法比较"
                        if record_pair:
                            result.comparisons.append(comparison_row(a, b, category=cat, label=label,
                                outcome="unverified", basis=reason))
                        if comparison_facts is None:
                            result.incomplete.append({"unit_id": a["unit_id"], "reason": reason, "category": "incomplete"})
                        continue
                    same = av == bv
                    fixture = "ISSUE-QTY"
                elif cat == "编号写法":
                    left_number, right_number = number_reads[a["id"]], number_reads[b["id"]]
                    same = left_number == right_number
                    compared_literals = {"left":left_number, "right":right_number}
                    fixture = "ISSUE-NUM-STYLE"
                else:
                    same = re.sub(r"\s", "", a["text"]) == re.sub(r"\s", "", b["text"])
                    fixture = "ISSUE-SPEC"
                if record_pair:
                    basis = "已按同一对象和同一标注项目配对"
                    result.comparisons.append(comparison_row(a, b, category=cat, label=label,
                        outcome="match" if same else "mismatch", basis=basis))
                if same:
                    continue
                quantity = {"quantity_difference": str(abs(av-bv)), "quantity_unit": a_unit} if cat == "数量与材料表" else {}
                issue(a, b, cat, f"同一对象同一计量单位 {a_unit or '未标单位'} 的两处数值不一致；按本地十进制比较，不推断为件数" if cat == "数量与材料表" else "同一对象原文中的唯一编号字面不同" if cat == "编号写法" else "同一对象的两处标注写法或规格不一致", fixture, compared_literals=compared_literals, **quantity)
            for f in selected:
                if f["id"] in candidates and f["id"] not in paired and cat != "编号写法":
                    pending(f, UNPAIRED_REASON)

        if comparison_facts is not None:
            return result

        scope = absence_scope(units, facts, import_diagnostics=import_scope_diagnostics(self.store, version_id))
        reference_diagnostics = []
        homes = {uid: verified_sheet_number(unit) for uid, unit in units.items()}
        for fact in [f for f in facts if category(f) == "详图索引断链"]:
            if _uncertain(fact):
                pending(fact, "详图索引看不清，待确认")
                continue
            reference = drawing_reference(fact, home_number=homes.get(fact["unit_id"]))
            if reference is None:
                if visible_detail_hint(fact):
                    pending(fact, NO_REFERENCE_REASON)
                continue
            resolved = resolve_reference(reference, units)
            reference_diagnostics.append(resolved)
            if resolved["status"] != "resolved":
                target_no = reference.get("target")
                listed = [n for n in scope["listed_numbers"] if n == target_no or
                          (target_no and reference.get("target_basis") != SAME_VOLUME_SHORTHAND
                           and not reference.get("confirmed_short_reference")
                           and any(n.endswith(sep + target_no) for sep in "-_./"))]
                if resolved["status"] == "missing" and scope["closed"] and len(listed) == 1:
                    issue(fact, None, "详图索引断链", f"完整目录与已核实实到身份中缺少引用图号 {listed[0]}",
                          "ISSUE-INDEX-BROKEN", reference_target=listed[0])
                else:
                    reason = resolved.get("reason") or "引用图纸身份未核实，待确认"
                    if resolved["status"] == "missing":
                        reason = OPEN_SCOPE_REASON
                    if not fact.get("confirmed_short_reference"):
                        pending(fact, reason)
                    result.incomplete.append({"unit_id":fact["unit_id"], "fact_id":fact["id"],
                                              "reason":reason, "category":"incomplete"})
                continue
            detail_no = resolved.get("detail")
            if not detail_no:
                continue
            targets = [units[uid] for uid in resolved["unit_ids"]]
            target_facts = [f for f in facts if f["unit_id"] in resolved["unit_ids"] and
                            str(f.get("detail_number", "")) == str(detail_no) and not _uncertain(f)]
            if target_facts:
                continue
            target_no = resolved["resolved_number"]
            if all((u.get("metadata") or {}).get("detail_inventory_complete") for u in targets):
                issue(fact, None, "详图索引断链", f"目标图 {target_no} 存在，但无详图 {detail_no}",
                      "ISSUE-INDEX-BROKEN", reference_target=target_no, reference_detail=detail_no)
            else:
                pending(fact, f"目标图 {target_no} 的详图 {detail_no} 未核实，待确认")
                result.incomplete.append({"unit_id":fact["unit_id"], "fact_id":fact["id"],
                                          "reason":"目标详图清单未完整核实", "category":"incomplete"})
        result.detail = {"engine": "rules_b", "model_calls": 0,
                         "reference_identity_scope":scope, "references":reference_diagnostics, "number_readings":number_diagnostics,
                         "volume_references": [{"unit_id": f["unit_id"], "volume": volume, "text": f["text"]}
                                               for f in facts if (volume := volume_reference(f["text"]))]}
        return result
