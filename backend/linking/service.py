"""关联生成与同名歧义（W7-Q2=B）处理。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from backend.store import BBox, Evidence, Repository, Source
from backend.evidence_selection import selected_facts, reliable_fact, usable_title_identity
from backend.references import BARE_VOLUME, SAME_VOLUME_RANGE, drawing_reference, resolve_reference, verified_sheet_number, volume_reference

LINK_KIND_TITLEBLOCK = "titleblock"
LINK_KIND_CATALOG_REF = "catalog_ref"
LINK_KIND_DETAIL_INDEX = "detail_index"
LINK_KIND_SHARED_NUMBER = "shared_number"

# 同名歧义待确认定稿文案（W7-Q2=B）
SAME_NAME_PENDING_REASON = "两处同名，无法确认是否为同一处，请核对"

_DRAWING_NO_RE = re.compile(r"^[A-Za-z]+-\d+[A-Za-z]?$")
_PURE_NUMBER_RE = re.compile(r"^\d+(?:\.\d+)?$")


_NAMED_OBJECT = re.compile(r"^(?:基础|支架|构件|柱|梁|桩|设备|管线|回路|柜|箱变)[A-Za-z]+[-]?\d+[A-Za-z]?$")


def same_name_pairs(facts):
    """Separate named objects from duplicate extraction of the same label."""
    groups = {}
    for fact in facts:
        text = (fact.get("text") or "").strip()
        if (not reliable_fact(fact) or fact.get("unit") or
                fact["x0"] >= fact["x1"] or fact["y0"] >= fact["y1"] or
                not (_NAMED_OBJECT.fullmatch(text) or (
                    fact.get("category") in {"对象名称", "构件名称", "设备名称"}
                    and 2 <= len(text) <= 24 and not re.search(r"[，。；：,;:]", text))) or
                drawing_reference(fact) or volume_reference(text)):
            continue
        groups.setdefault((fact["unit_id"], text), []).append(fact)
    for group in groups.values():
        left = group[0]
        for right in group[1:]:
            if (left["x1"] <= right["x0"] or right["x1"] <= left["x0"] or
                    left["y1"] <= right["y0"] or right["y1"] <= left["y0"]):
                yield left, right
                break


@dataclass
class SameNameAmbiguity:
    unit_id: str
    text: str
    left_bbox: dict[str, float]
    right_bbox: dict[str, float]
    pending_id: str | None = None


@dataclass
class LinkingResult:
    associations: list[dict[str, Any]] = field(default_factory=list)
    same_name_pending: list[SameNameAmbiguity] = field(default_factory=list)
    rejected_name_only: int = 0
    rejected_digit_only: int = 0
    incomplete: list[dict[str, Any]] = field(default_factory=list)


class LinkingService:
    """根据图签、目录引用、详图索引、共有编号建立关联；同名歧义转 pending。"""

    def __init__(self, store: Repository):
        self.store = store

    def link_version(self, version_id: str, *, persist: bool = True,
                     reference_fact_id: str | None = None) -> LinkingResult:
        if persist and reference_fact_id is None:
            from backend.catalog import CatalogService
            CatalogService(self.store).refresh_catalog_evidence(version_id)
        unit_map, facts = selected_facts(self.store, version_id)
        units = list(unit_map.values())
        facts = [fact for fact in facts if reliable_fact(fact)]
        result = LinkingResult()

        if not units:
            result.incomplete.append({
                "unit_id": None,
                "reason": "无图纸单元，关联跳过",
                "category": "incomplete",
            })
            return result

        by_id = {u["id"]: u for u in units}
        # 提取 incomplete
        for u in units:
            meta = u.get("metadata") or {}
            if meta.get("extract_status") == "incomplete":
                result.incomplete.append({
                    "unit_id": u["id"],
                    "reason": meta.get("extract_incomplete_reason") or "识别未完成",
                    "category": "incomplete",
                })

        # ---- 同名歧义（W7-Q2=B）：不报疑似，写 1 条 pending ----
        result.same_name_pending = self._handle_same_name(
            version_id, units, facts, persist=persist and reference_fact_id is None)

        # ---- 合法关联 ----
        pairs: dict[tuple[str, str], dict[str, Any]] = {}

        def add_pair(left_id: str, right_id: str, kind: str, basis: str, confidence: float, reference=None) -> None:
            if left_id == right_id:
                return
            a, b = sorted([left_id, right_id])
            key = (a, b)
            prev = pairs.get(key)
            if prev and float(prev["details"].get("confidence") or 0) >= confidence:
                return
            pairs[key] = {
                "left_unit_id": a,
                "right_unit_id": b,
                "kind": kind,
                "details": {
                    "basis": basis,
                    "basis_kind": kind,
                    "confidence": confidence,
                    **({"reference": reference} if reference else {}),
                },
            }

        # 1) 图签 / 目录：同图号（结构化图号）互相关联
        by_number: dict[str, list[str]] = {}
        for u in units:
            if not usable_title_identity(u):
                continue
            dn = (u.get("drawing_number") or "").strip()
            if not dn or not _is_structured_drawing_number(dn):
                continue
            meta = u.get("metadata") or {}
            if meta.get("out_of_scope") or meta.get("is_catalog_sheet"):
                continue
            by_number.setdefault(dn, []).append(u["id"])
        for dn, ids in by_number.items():
            for i, left in enumerate(ids):
                for right in ids[i + 1 :]:
                    add_pair(left, right, LINK_KIND_TITLEBLOCK, f"图签图号一致：{dn}", 0.95)

        # 目录引用：目录页列出的图号 → 对应单元
        catalog_units = [u for u in units if (u.get("metadata") or {}).get("is_catalog_sheet") and
                         (u.get("metadata") or {}).get("extract_status") != "incomplete" and
                         not (u.get("metadata") or {}).get("out_of_scope")]
        for cat in catalog_units:
            listed = list((cat.get("metadata") or {}).get("catalog_listed_numbers") or [])
            for num in listed:
                num = str(num).strip()
                if not num:
                    continue
                for u in units:
                    if not usable_title_identity(u):
                        continue
                    if u["id"] == cat["id"]:
                        continue
                    if (u.get("drawing_number") or "").strip() == num:
                        add_pair(
                            cat["id"],
                            u["id"],
                            LINK_KIND_CATALOG_REF,
                            f"目录引用图号：{num}",
                            0.9,
                        )

        # 2) Unique literal sheet identity, not validation of detail suitability.
        homes = {uid: verified_sheet_number(unit) for uid, unit in unit_map.items()}
        seen_ranges = set()
        for fact in facts:
            reference = drawing_reference(fact, home_number=homes.get(fact["unit_id"]))
            if reference is None:
                continue
            if reference.get("target_basis") == SAME_VOLUME_RANGE:
                key = (fact["unit_id"], tuple(reference["bbox"].values()), tuple(reference["targets"]))
                if key in seen_ranges:
                    continue
                seen_ranges.add(key)
            resolved = resolve_reference(reference, unit_map)
            if resolved["status"] != "resolved":
                result.incomplete.append({"unit_id":fact["unit_id"], "fact_id":fact["id"],
                                          "reason":resolved["reason"], "category":"incomplete"})
                continue
            if resolved.get("match_basis") == BARE_VOLUME:
                add_pair(fact["unit_id"], resolved["unit_ids"][0], LINK_KIND_CATALOG_REF,
                         f"原文卷册简称对应卷册检索号：{resolved['resolved_number']}（未指向单张图）",
                         .88, reference=resolved)
                continue
            numbers = resolved.get("resolved_numbers") or [resolved["resolved_number"]]
            for uid, number in zip(resolved["unit_ids"], numbers):
                add_pair(fact["unit_id"], uid, LINK_KIND_DETAIL_INDEX,
                         f"引用原文对应图纸身份：{number}（未核实做法适用性）",
                         .88, reference=resolved)

        # 3) 共有编号：两侧事实同为结构化编号（非纯数字），且非「仅同名」
        number_facts: dict[str, list[dict]] = {}
        for fact in facts:
            text = (fact.get("text") or "").strip()
            if not _is_structured_drawing_number(text) and not _looks_like_shared_code(text):
                continue
            if _PURE_NUMBER_RE.match(text):
                result.rejected_digit_only += 1
                continue
            number_facts.setdefault(text, []).append(fact)
        for text, group in number_facts.items():
            unit_ids = sorted({f["unit_id"] for f in group})
            if len(unit_ids) < 2:
                continue
            for i, left in enumerate(unit_ids):
                for right in unit_ids[i + 1 :]:
                    add_pair(
                        left,
                        right,
                        LINK_KIND_SHARED_NUMBER,
                        f"共有编号：{text}",
                        0.8,
                    )

        # 显式拒绝：仅凭相同汉字名或相同纯数字不得建关联（计数供测试）
        name_groups: dict[str, list[dict]] = {}
        for fact in facts:
            text = (fact.get("text") or "").strip()
            if not text or fact.get("unit"):
                continue
            if _is_structured_drawing_number(text) or _looks_like_shared_code(text):
                continue
            if _PURE_NUMBER_RE.match(text):
                result.rejected_digit_only += 1
                continue
            if re.search(r"[\u4e00-\u9fff]", text):
                name_groups.setdefault(text, []).append(fact)
        for text, group in name_groups.items():
            unit_ids = {f["unit_id"] for f in group}
            if len(group) >= 2:
                # 同名不得建关联
                result.rejected_name_only += 1

        if persist:
            # Reconcile only records carrying this generator's provenance.
            # Failed-page links must not survive in the live association table;
            # manual/external records without this marker remain untouched.
            generated_kinds = {LINK_KIND_TITLEBLOCK, LINK_KIND_CATALOG_REF, LINK_KIND_DETAIL_INDEX, LINK_KIND_SHARED_NUMBER}
            retained = {}
            with self.store._transaction():
                previous = self.store.list_records("associations", version_id=version_id)
                if reference_fact_id is not None:
                    affected = {key for key, item in pairs.items()
                                if (item["details"].get("reference") or {}).get("fact_id") == reference_fact_id}
                    affected.update(tuple(sorted((row["left_unit_id"], row["right_unit_id"]))) for row in previous
                                    if ((row["details"] or {}).get("reference") or {}).get("fact_id") == reference_fact_id)
                    pairs = {key: item for key, item in pairs.items() if key in affected}
                    previous = [row for row in previous
                                if tuple(sorted((row["left_unit_id"], row["right_unit_id"]))) in affected]
                for old in previous:
                    details = old.get("details") or {}
                    if old["kind"] not in generated_kinds or details.get("basis_kind") != old["kind"]:
                        continue
                    key = tuple(sorted((old["left_unit_id"], old["right_unit_id"])))
                    current = pairs.get(key)
                    if (current and old["kind"] == current["kind"] and details == current["details"] and key not in retained):
                        retained[key] = old
                    else:
                        self.store._connection.execute("DELETE FROM associations WHERE id=?", (old["id"],))
                for key, item in pairs.items():
                    row = retained.get(key) or self.store.add_association(
                        version_id,
                        item["left_unit_id"],
                        item["right_unit_id"],
                        item["kind"],
                        item["details"],
                    )
                    result.associations.append(row)
        else:
            result.associations = list(pairs.values())

        return result

    def _handle_same_name(
        self,
        version_id: str,
        units: list[dict],
        facts: list[dict],
        *,
        persist: bool,
    ) -> list[SameNameAmbiguity]:
        """同名 ≥2 且无其它身份依据 → 每组写 1 条 pending，不报 issue。"""
        # A waiting machine question may be reused; an answered question is an
        # audit record, not proof about the current (unstored) right-hand pair.
        # Preserve all historical duplicates and human decisions. Stable existing
        # selection stops repeated review/resume from adding another question.
        fields = ("unit_id", "source", "text", "x0", "y0", "x1", "y1")
        waiting = {}
        if persist:
            records = self.store.list_records("pending_confirmations", version_id=version_id)
            # Repository lists pending records in insertion (rowid) order.
            for row in records:
                if (row["reason"] == SAME_NAME_PENDING_REASON and row["status"] == "待确认"
                        and row.get("confirmed_text") is None):
                    waiting.setdefault(tuple(row[field] for field in fields), row)

        out: list[SameNameAmbiguity] = []
        for left, right in same_name_pairs(facts):
            unit_id, text = left["unit_id"], left["text"].strip()
            left_bb = _bbox_dict(left)
            right_bb = _bbox_dict(right)
            pending_id = None
            if persist:
                evidence = Evidence(
                    unit_id,
                    Source(left.get("source") or Source.VISION.value),
                    left["text"],
                    BBox(**left_bb),
                )
                columns = evidence.columns()
                key = tuple(columns[field] for field in fields)
                row = waiting.get(key)
                if row is None:
                    row = self.store.create_pending(version_id, evidence, SAME_NAME_PENDING_REASON)
                    waiting[key] = row
                pending_id = row["id"]
            out.append(
                SameNameAmbiguity(
                    unit_id=unit_id,
                    text=text,
                    left_bbox=left_bb,
                    right_bbox=right_bb,
                    pending_id=pending_id,
                )
            )
        return out


def _bbox_dict(fact: dict) -> dict[str, float]:
    return {
        "x0": float(fact["x0"]),
        "y0": float(fact["y0"]),
        "x1": float(fact["x1"]),
        "y1": float(fact["y1"]),
    }


def _is_structured_drawing_number(text: str) -> bool:
    return bool(_DRAWING_NO_RE.match(text.strip()))


def _looks_like_shared_code(text: str) -> bool:
    """共有编号：字母+数字组合，非纯数字、非纯中文名。"""
    t = text.strip()
    if not t or _PURE_NUMBER_RE.match(t):
        return False
    if _is_structured_drawing_number(t):
        return True
    # 例 XB-03、F1a 带前缀编码
    return bool(re.match(r"^[A-Za-z]{1,6}-?\d+[A-Za-z0-9]*$", t))
