"""规则公共：容差读取、事实分类、写 issue / pending。"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from backend.store import BBox, Evidence, Repository, Source
from .unit_declaration import LENGTH_SCALE_MM, literal_measurement

PLACEHOLDER_BBOX = BBox(0.0, 0.0, 0.0, 0.0)
REVIEW_STATUS_SUSPECTED = "疑似"

_DIM_MUL_RE = re.compile(r"[×xX]")
_ELEV_NUM_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)")
def length_scale_mm(fact):
    _, unit = literal_measurement(fact.get("text"))
    return LENGTH_SCALE_MM.get(unit)


def visible_measurement_values(fact):
    literal, _ = literal_measurement(fact.get("text"))
    if literal is None:
        return []
    literal = literal.lstrip("ΦφØørR")
    if "±" in literal:
        if not re.fullmatch(r"±0(?:\.0+)?", literal):
            return []
        literal = literal[1:]
    return [Decimal(part) for part in _DIM_MUL_RE.split(literal)]


def load_tolerances(store: Repository, task_id: str | None, settings: dict[str, Any] | None) -> dict[str, float]:
    """优先任务容差快照（Q-01）；否则 settings；再否则 store 当前设置。测试不得写死唯一分支。"""
    snap: dict[str, Any] = {}
    if task_id:
        try:
            task = store.get("review_tasks", task_id)
            snap = dict(task.get("tolerance_snapshot") or {})
        except Exception:
            snap = {}
    base = dict(settings or {})
    if not base:
        try:
            base = store.get_settings()
        except Exception:
            base = {}
    dim = snap.get("dimension_tolerance_mm", base.get("dimension_tolerance_mm"))
    elv = snap.get("elevation_tolerance_m", base.get("elevation_tolerance_m"))
    if dim is None:
        dim = base.get("dimension_tolerance_mm", 1)
    if elv is None:
        elv = base.get("elevation_tolerance_m", 0.005)
    return {
        "dimension_tolerance_mm": float(dim),
        "elevation_tolerance_m": float(elv),
    }


def classify_fact(fact: dict[str, Any], *, hint: str | None = None) -> str | None:
    if hint:
        return hint
    explicit = fact.get("category")
    if explicit:
        return str(explicit)
    unit = (fact.get("unit") or "").strip().lower()
    text = (fact.get("text") or "").strip()
    if re.search(r"(?:YJV|TMY|DN\d|材料表|数量)", text, re.I):
        return None
    if unit in {"mm", "cm"} or re.search(r"\d\s*[×xX]\s*\d", text):
        return "尺寸"
    if unit == "m":
        return "标高"
    if text.startswith("±") or re.fullmatch(r"\d+\.\d{3}", text):
        return "标高"
    if unit == "" and re.fullmatch(r"\d+(?:\.\d+)?", text):
        # 无单位纯数字：尺寸候选（缺单位路径）
        return "尺寸"
    if re.search(r"\d+\s*mm\b", text, re.I) or text.lower().endswith("mm"):
        return "尺寸"
    return None


def parse_dimension_values(text: str, numeric_value: str | None = None) -> list[Decimal] | None:
    if numeric_value is not None and str(numeric_value).strip() != "":
        try:
            return [Decimal(str(numeric_value))]
        except InvalidOperation:
            pass
    raw = (text or "").strip()
    raw = re.sub(r"[Mm]{2}$", "", raw).strip()
    if not raw:
        return None
    parts = _DIM_MUL_RE.split(raw)
    values: list[Decimal] = []
    for part in parts:
        part = part.strip()
        part = re.sub(r"[^\d.+-]", "", part)
        if not part:
            continue
        try:
            values.append(Decimal(part))
        except InvalidOperation:
            return None
    return values or None


def parse_elevation_value(text: str, numeric_value: str | None = None) -> Decimal | None:
    if numeric_value is not None and str(numeric_value).strip() != "":
        try:
            return Decimal(str(numeric_value))
        except InvalidOperation:
            pass
    raw = (text or "").strip()
    if "=" in raw:
        raw = raw.split("=")[-1].strip()
    m = _ELEV_NUM_RE.search(raw.replace("±", ""))
    if not m:
        return None
    try:
        return Decimal(m.group(1))
    except InvalidOperation:
        return None


def abs_diff(a: Decimal, b: Decimal) -> Decimal:
    d = a - b
    return d.copy_abs()


def exceeds_tolerance(diff: Decimal, tolerance: float) -> bool:
    """差值 ≤ 容差不报；> 才报（第 4 波裁定）。"""
    return diff > Decimal(str(tolerance))


def bbox_from_fact(fact: dict[str, Any]) -> BBox:
    return BBox(float(fact["x0"]), float(fact["y0"]), float(fact["x1"]), float(fact["y1"]))


def evidence_from_fact(fact: dict[str, Any], *, source: Source = Source.VISION) -> Evidence:
    return Evidence(fact["unit_id"], Source(fact.get("source") or source), fact.get("text") or "", bbox_from_fact(fact))


def _declaration_snapshot(rule):
    return [{k: v for k, v in row.items() if k != "fact_id"} for row in rule.get("unit_declarations") or []]


def write_issue(
    store: Repository,
    version_id: str,
    *,
    title: str,
    left: Evidence,
    right: Evidence,
    category: str,
    left_value: str,
    right_value: str,
    basis: str,
    fixture_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict:
    rule_result = {
        "category": category,
        "left_value": left_value,
        "right_value": right_value,
        "basis": basis,
        "summary": title,
    }
    if fixture_id:
        rule_result["fixture_id"] = fixture_id
    if extra:
        rule_result.update(extra)
    # Recalculation must reuse identical manual decisions and be idempotent on resume.
    for existing in store.list_records("issues", version_id=version_id):
        er = existing.get("rule_result") or {}
        if er.get("category") != category or er.get("left_value") != left_value or er.get("right_value") != right_value:
            continue
        # A different default is new comparison evidence. Never attach an old
        # manual outcome to it simply because the two printed numbers match.
        if _declaration_snapshot(er) != _declaration_snapshot(rule_result):
            continue
        if all(existing.get(key) == value for key, value in {**left.columns("left_"), **right.columns("right_")}.items()):
            return existing
    issue = store.create_issue(
        version_id,
        title,
        left,
        right,
        rule_result=rule_result,
        primary_opinion={"engine": "rules_a"},
        review_opinion=None,
    )
    # 默认已是疑似；显式确保
    if issue.get("review_status") != REVIEW_STATUS_SUSPECTED:
        issue = store.update_issue(issue["id"], review_status=REVIEW_STATUS_SUSPECTED)
    return issue


def pair_by_row(facts: list[dict[str, Any]], *, y_tol: float = 0.025) -> list[tuple[dict, dict]]:
    """同一图纸内按行（y 接近）两两配对，避免跨行串框。"""
    remaining = sorted(facts, key=lambda f: (float(f["y0"]), float(f["x0"])))
    pairs: list[tuple[dict, dict]] = []
    used: set[str] = set()
    for i, a in enumerate(remaining):
        if a["id"] in used:
            continue
        best = None
        best_dx = None
        for b in remaining[i + 1 :]:
            if b["id"] in used:
                continue
            if abs(float(a["y0"]) - float(b["y0"])) > y_tol:
                continue
            dx = abs(float(a["x0"]) - float(b["x0"]))
            if dx < 1e-9:
                continue
            if best is None or dx < best_dx:
                best = b
                best_dx = dx
        if best is not None:
            used.add(a["id"])
            used.add(best["id"])
            pairs.append((a, best))
    return pairs


def write_pending(store, version_id, evidence, reason):
    for item in store.list_records("pending_confirmations", version_id=version_id):
        if item["reason"] == reason and all(item.get(k) == v for k, v in evidence.columns().items()):
            return item
    return store.create_pending(version_id, evidence, reason)


def comparable_pairs(facts):
    """Index only necessary conditions for identity on a read-only fact snapshot.

    `_same_object` can succeed through exact specific object IDs, exact specific
    comparison keys, or its legacy canonical-code branch. Index all three, then
    apply the unchanged predicates. Never infer identity from row proximity.
    """
    from .rules_b import _canonical_code, _same_object, _specific_identity, _uncertain, category

    facts = list(facts)
    indexes = {"object": {}, "comparison": {}, "code": {}}
    memberships = {}
    reliable = []
    for index, fact in enumerate(facts):
        if _uncertain(fact):
            continue  # Every original combination containing this fact is rejected.
        reliable.append(index)
        keys = []
        if fact.get("identity_confirmed"):
            for kind, field in (("object", "object_id"), ("comparison", "comparison_key")):
                value = fact.get(field)
                if _specific_identity(value):
                    indexes[kind].setdefault(value, []).append(index)
                    keys.append((kind, value))
        code = _canonical_code(fact.get("text") or "")
        if code is not None:
            indexes["code"].setdefault(code, []).append(index)
            # The original fallback tests only the LEFT category. Right-hand
            # facts must stay indexed even when their category is different.
            if category(fact) == "编号写法":
                keys.append(("code", code))
        if keys:
            memberships[index] = keys

    # Preserve combinations(facts, 2)'s order and references, including pairs
    # discoverable through more than one identity path.
    for index in reliable:
        candidates = set()
        for kind, key in memberships.get(index, ()):
            candidates.update(other for other in indexes[kind][key] if other > index)
        left = facts[index]
        for other in sorted(candidates):
            right = facts[other]
            if _uncertain(left) or _uncertain(right) or not _same_object(left, right):
                continue
            if left.get("comparison_key") and right.get("comparison_key") and left["comparison_key"] != right["comparison_key"]:
                continue
            yield left, right


def comparison_row(left, right, *, category, label, outcome, basis,
                   left_value=None, right_value=None, unit_declarations=()):
    def evidence(fact):
        return {"text": fact["text"], "source": fact["source"],
                "bbox": {k: fact[k] for k in ("x0", "y0", "x1", "y1")}}
    return {"category": category, "label": label, "outcome": outcome, "basis": basis,
            "left_unit_id": left["unit_id"], "right_unit_id": right["unit_id"],
            "left_value": left_value if left_value is not None else left["text"],
            "right_value": right_value if right_value is not None else right["text"],
            "left_evidence": evidence(left), "right_evidence": evidence(right),
            **({"unit_declarations": unit_declarations} if unit_declarations else {})}


def compared_object(left, right, category):
    for field in ("object_id", "comparison_key"):
        value = left.get(field)
        if value and value == right.get(field):
            return value
    return category
