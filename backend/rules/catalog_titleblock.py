"""目录与图签、图签编号前缀。"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from backend.store import BBox, Evidence, Repository, Source, StoreError
from backend.title_identity import has_manual_number, manual_number_mode
from backend.references import _independent_title, absence_scope, import_scope_diagnostics

from .common import PLACEHOLDER_BBOX, write_issue

CATEGORY_CATALOG = "目录与图签不符"
CATEGORY_COUNT = "目录张数"
CATEGORY_PREFIX = "图签编号前缀"
TITLE_UNVERIFIED = "图纸身份尚未核实，目录缺图与张数核对未完成"
CATALOG_SCOPE_INCOMPLETE = "目录缺图与张数核对范围未闭合："


def _title_evidence(store, unit, printed):
    """Use the actual selected fact, never relabel a CAD original as vision."""
    meta = unit.get("metadata") or {}
    resolution = meta.get("title_number_resolution") or {}
    selected = resolution.get("selected_evidence") or {}
    try:
        if has_manual_number(unit):
            # Human edits own the value; the previous field position remains a
            # location anchor, not proof that CAD contained the edited string.
            prior = resolution.get("prior_cad_evidence") or {}
            box = prior.get("bbox") or meta.get("title_bbox")
            return Evidence(unit["id"], Source.USER, printed, BBox(**box) if box else PLACEHOLDER_BBOX)
        if resolution.get("status") == "cad_selected":
            if resolution.get("selected_drawing_number") != printed or resolution.get("selected_source") != Source.CAD_TEXT.value:
                return None
            fact = store.get("facts", selected.get("fact_id"))
            if fact["unit_id"] != unit["id"] or fact["version_id"] != unit["version_id"] or fact["source"] != Source.CAD_TEXT.value:
                return None
            if fact["text"] != selected.get("text") or any(fact[key] != (selected.get("bbox") or {}).get(key) for key in ("x0", "y0", "x1", "y1")):
                return None
            return Evidence(unit["id"], Source.CAD_TEXT, fact["text"], BBox(**{key:fact[key] for key in ("x0", "y0", "x1", "y1")}))
        if resolution.get("status") == "local_selected":
            title = (meta.get("local_sheet") or {}).get("title") or {}
            if title.get("number") != printed or title.get("value") != selected:
                return None
            return Evidence(unit["id"], Source(selected["source"]), selected["text"], BBox(**selected["bbox"]))
        box = meta.get("title_bbox")
        return Evidence(unit["id"], Source.VISION, printed, BBox(**box) if box else PLACEHOLDER_BBOX)
    except (StoreError, TypeError, ValueError):
        return None


def _title_diagnostic(unit):
    meta = unit.get("metadata") or {}
    result = {key:meta[key] for key in ("vision_title_drawing_number", "title_number_resolution") if key in meta}
    mode = manual_number_mode(unit)
    if mode:
        result["manual_number_confirmation_mode"] = mode
    return result


def local_catalog_checks(store, version_id, units, catalogs, persist=False):
    """Compare a unique literal drawing name within one source PDF."""
    from backend.extract.local_sheet import compact

    actual = defaultdict(list)
    for unit in units:
        meta = unit.get("metadata") or {}
        if meta.get("is_catalog_sheet") or meta.get("out_of_scope") or meta.get("title_identity_uncertain"):
            continue
        title = (meta.get("local_sheet") or {}).get("title") or {}
        name = title.get("name")
        if name and title["number"] == unit.get("drawing_number"):
            actual[unit["original_id"], compact(name["text"])].append((unit, title))

    listed = defaultdict(list)
    for catalog in catalogs:
        meta = catalog.get("metadata") or {}
        for row in ((meta.get("local_sheet") or {}).get("catalog") or {}).get("rows", []):
            if row.get("name"):
                listed[catalog["original_id"], compact(row["name"]["text"])].append((catalog, row))

    issues, comparisons = [], []
    for key, candidates in listed.items():
        matches = actual.get(key, [])
        if len(candidates) != 1 or len(matches) != 1:
            continue
        catalog, row = candidates[0]
        unit, title = matches[0]
        left_raw = row["value"]
        left = Evidence(catalog["id"], Source(left_raw["source"]), left_raw["text"], BBox(**left_raw["bbox"]))
        right = _title_evidence(store, unit, title["number"])
        if right is None:
            continue
        same = row["number"] == title["number"]
        comparisons.append({"catalog_unit_id": catalog["id"], "unit_id": unit["id"],
                            "catalog_number": row["number"], "title_number": title["number"],
                            "category": "目录与图签", "label": row["name"]["text"],
                            "left_unit_id": catalog["id"], "right_unit_id": unit["id"],
                            "left_value": row["number"], "right_value": title["number"],
                            "left_evidence": left_raw, "right_evidence": {
                                "text": right.text, "source": right.source.value, "bbox": vars(right.bbox)},
                            "basis": "按同一文件中唯一对应的图名配对，逐字符核对图号",
                            "outcome": "match" if same else "mismatch"})
        if not same:
            message = f"目录与图签图号不符：{row['number']} / {title['number']}"
            issues.append(write_issue(store, version_id, title=message, left=left, right=right,
                category=CATEGORY_CATALOG, left_value=row["number"], right_value=title["number"],
                basis="同一源文件中，目录与图签的原文图名唯一对应，图号逐字符不一致",
                extra={"match_basis": "unique_literal_name_in_source", "object_label": row["name"]["text"],
                       "catalog_name_evidence": row["name"],
                       "title_name_evidence": title["name"]}) if persist else
                {"title": message, "category": CATEGORY_CATALOG})
    return issues, comparisons


def run_catalog_titleblock(
    store: Repository,
    version_id: str,
    *,
    expected_prefix: str | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    units = store.list_records("drawing_units", version_id=version_id)
    issues: list[dict] = []
    incomplete: list[dict] = []

    catalog_units = [u for u in units if (u.get("metadata") or {}).get("is_catalog_sheet")]
    if not catalog_units:
        # 无目录页：仅做前缀；目录核对跳过并记 incomplete（不得假称已核对）
        incomplete.append({
            "unit_id": None,
            "reason": "无目录页，目录与图签核对跳过",
            "category": "incomplete",
        })
        prefix_issues = _check_prefix(store, version_id, units, expected_prefix, persist=persist, incomplete=incomplete)
        return {"issues": prefix_issues, "incomplete": incomplete}

    local_issues, local_comparisons = local_catalog_checks(store, version_id, units, catalog_units, persist)
    issues.extend(local_issues)
    catalog_ids = {unit["id"] for unit in catalog_units}
    catalog_numbers = {unit.get("drawing_number") for unit in catalog_units if unit.get("drawing_number")}
    volume_numbers = {number for unit in catalog_units for number in (unit.get("metadata") or {}).get("catalog_volume_numbers", [])}
    sources: dict[str, dict] = {}
    all_complete = True
    for cat in catalog_units:
        meta = cat.get("metadata") or {}
        raw = meta.get("catalog_listed_numbers")
        valid = isinstance(raw, list) and all(isinstance(number, str) for number in raw)
        # Explicit stored legacy lists are preserved as trusted manual/replay input.
        # New recognition always writes catalog_complete explicitly.
        complete = valid and bool(raw) and meta.get("catalog_complete", True) is True
        if meta.get("extract_status") == "incomplete":
            complete = False
            valid = False
        if not complete:
            all_complete = False
            incomplete.append({"unit_id":cat["id"], "reason":meta.get("catalog_incomplete_reason") or
                ("目录页缺少 catalog_listed_numbers，目录核对未完成" if not valid else "目录页未完整读清，目录核对未完成"), "category":"incomplete"})
        if valid:
            for number in raw:
                number = number.strip()
                if number and number not in catalog_numbers and number not in volume_numbers:
                    sources.setdefault(number, cat)
    listed_set = set(sources)
    existing: dict[str, dict] = {}
    in_scope: dict[str, dict] = {}
    identities_complete = True
    facts = store.list_records("facts", version_id=version_id)
    for unit in units:
        meta = unit.get("metadata") or {}
        if unit["id"] in catalog_ids:
            continue
        number = (unit.get("drawing_number") or "").strip()
        manual_title = has_manual_number(unit) and bool(number)
        invalid_anchor = ((meta.get("title_number_resolution") or {}).get("status") == "cad_selected"
                          and _title_evidence(store, unit, number) is None)
        identity_gap = (not number or invalid_anchor or meta.get("extract_status") == "incomplete"
                        or (not manual_title and (meta.get("no_title_block") or meta.get("title_identity_uncertain"))))
        if identity_gap and not _independent_title(unit, facts):
            identities_complete = False
            incomplete.append({"unit_id":unit["id"], "reason":TITLE_UNVERIFIED, "category":"incomplete"})
        if not number:
            continue
        existing[number] = unit
        if not meta.get("out_of_scope"):
            in_scope[number] = unit
    # Positive directory evidence can expose a missing sheet, but unread identities
    # could be that sheet. Absence from a partial collection is never an "unlisted" finding.
    # Existence is not disproved by an unlisted visual title such as a model's
    # "11#" reading. Preserve positive extra/prefix checks; absence and count
    # need the stronger catalogue/independent-identity boundary.
    scope = absence_scope({u["id"]:u for u in units}, facts,
                          import_diagnostics=import_scope_diagnostics(store, version_id))
    if not scope["closed"]:
        incomplete.append({"unit_id":None, "reason":CATALOG_SCOPE_INCOMPLETE + "；".join(scope["reasons"]),
                           "category":"incomplete"})
    missing = sorted(listed_set - set(existing)) if identities_complete and scope["closed"] else []
    extra = sorted(set(in_scope) - listed_set) if all_complete and identities_complete and scope["closed"] else []
    paired_mismatches = [item for item in local_comparisons if item["outcome"] == "mismatch"]
    paired_listed = {item["catalog_number"] for item in paired_mismatches}
    paired_actual = {item["title_number"] for item in paired_mismatches}
    missing = [number for number in missing if number not in paired_listed]
    extra = [number for number in extra if number not in paired_actual]

    def catalog_bbox(cat, number=None):
        meta = cat.get("metadata") or {}
        row = next((entry for entry in meta.get("catalog_entries", []) if entry.get("drawing_number") == number), None)
        box = (row or {}).get("bbox") or meta.get("catalog_bbox")
        return BBox(**{key:float(box[key]) for key in ("x0","y0","x1","y1")}) if box else PLACEHOLDER_BBOX

    for number in missing:
        cat = sources[number]
        title = f"目录列了但缺图 {number}"
        row = next((entry for entry in (cat.get("metadata") or {}).get("catalog_entries", [])
                    if entry.get("drawing_number") == number), {})
        left = Evidence(cat["id"], Source(row.get("source", Source.VISION)), number, catalog_bbox(cat, number))
        right = Evidence(cat["id"], Source.VISION, "（缺图）", catalog_bbox(cat, number))
        issues.append(write_issue(store, version_id, title=title, left=left, right=right,
            category=CATEGORY_CATALOG, left_value=number, right_value="",
            basis="目录中明确列出该图号，但已核实身份的版本图纸中无对应图纸",
            fixture_id="ISSUE-CAT-MISSING", extra={"catalog_unit_ids":[cat["id"]]}) if persist else
            {"title":title,"category":CATEGORY_CATALOG,"fixture_id":"ISSUE-CAT-MISSING"})
    for number in extra:
        unit = in_scope[number]
        cat = next((candidate for candidate in catalog_units if candidate["original_id"] == unit["original_id"]), catalog_units[0])
        meta = unit.get("metadata") or {}
        title_evidence = _title_evidence(store, unit, number)
        if title_evidence is None:
            incomplete.append({"unit_id":unit["id"], "reason":"图签所选原文证据无法复核，需确认", "category":"incomplete"})
            continue
        title = f"有图但目录未列 {number}"
        issues.append(write_issue(store, version_id, title=title,
            left=title_evidence,
            right=Evidence(cat["id"], Source.VISION, "完整目录集合未列", catalog_bbox(cat)),
            category=CATEGORY_CATALOG, left_value=number, right_value="",
            basis="所有已完整读取的目录合并后未列出该图号", fixture_id="ISSUE-CAT-EXTRA",
            extra={"catalog_unit_ids":sorted(catalog_ids), **_title_diagnostic(unit)}) if persist else
            {"title":title,"category":CATEGORY_CATALOG,"fixture_id":"ISSUE-CAT-EXTRA"})
    # A fully read physical row remains a sheet entry even when the original
    # directory repeats its number. Do not turn a content error into a false
    # "unread directory" or derive physical sheet count from a deduplicated set.
    physical_rows = [((cat.get("metadata") or {}).get("local_sheet") or {}).get("catalog")
                     for cat in catalog_units]
    counts_physical = all(reading and reading.get("complete") for reading in physical_rows)
    listed_count = (sum(row.get("number") not in catalog_numbers | volume_numbers
                        for reading in physical_rows for row in reading["rows"])
                    if counts_physical else len(listed_set))
    if all_complete and identities_complete and scope["closed"] and listed_count != len(in_scope):
        cat = catalog_units[0]
        title = "目录张数与实到图不一致（含缺图与未列）"
        issues.append(write_issue(store, version_id, title=title,
            left=Evidence(cat["id"], Source.VISION, "所有完整目录合并张数", catalog_bbox(cat)),
            right=Evidence(cat["id"], Source.VISION, f"listed={listed_count},present={len(in_scope)}", catalog_bbox(cat)),
            category=CATEGORY_COUNT, left_value=str(listed_count), right_value=str(len(in_scope)),
            basis=("已完整读取的目录物理图纸行数与实到图不一致（不含目录自身和卷册号）" if counts_physical else
                   "所有完整目录的图纸页号去重后与实到图不一致（不含目录自身和卷册号）"),
            fixture_id="ISSUE-CAT-COUNT", extra={"missing":missing,"extra":extra,"catalog_unit_ids":sorted(catalog_ids)}) if persist else
            {"title":title,"category":CATEGORY_COUNT,"fixture_id":"ISSUE-CAT-COUNT"})
    issues.extend(_check_prefix(store, version_id, units, expected_prefix, persist=persist, incomplete=incomplete))
    result = {"issues": issues, "incomplete": incomplete}
    if local_comparisons:
        result["local_comparisons"] = local_comparisons
    return result


def _check_prefix(
    store: Repository,
    version_id: str,
    units: list[dict],
    expected_prefix: str | None,
    *,
    persist: bool,
    incomplete: list | None = None,
) -> list[dict]:
    """图签编号前缀：优先读单元 metadata.title_drawing_number，否则 drawing_number。"""
    issues: list[dict] = []
    # 约定前缀：任务/设置可覆写；夹具默认 S
    prefix = expected_prefix
    if not prefix:
        for u in units:
            p = (u.get("metadata") or {}).get("expected_number_prefix")
            if p:
                prefix = str(p)
                break
    if not prefix:
        return []  # No project-specific convention has been established; never invent an S prefix.

    for u in units:
        meta = u.get("metadata") or {}
        if meta.get("is_catalog_sheet") or meta.get("out_of_scope") or (meta.get("title_identity_uncertain") and not has_manual_number(u)):
            continue
        resolved = meta.get("title_number_resolution") or {}
        if has_manual_number(u) or resolved.get("status") == "cad_selected":
            printed = (u.get("drawing_number") or "").strip()
        else:
            printed = (meta.get("title_drawing_number") or u.get("drawing_number") or "").strip()
        if not printed:
            continue
        # 仅检查显式要求前缀校验的单元，或所有带 title_drawing_number 的
        must = bool(meta.get("check_prefix")) or meta.get("title_drawing_number")
        if not must and not meta.get("check_prefix"):
            # 默认：对所有非空图号做前缀检查
            must = True
        if not must:
            continue
        ok = printed.upper().startswith(str(prefix).upper())
        if ok:
            continue
        title = f"图签编号前缀不符：{printed}（约定 {prefix}）"
        left = _title_evidence(store, u, printed)
        if left is None:
            if incomplete is not None:
                incomplete.append({"unit_id":u["id"], "reason":"图签所选原文证据无法复核，需确认", "category":"incomplete"})
            continue
        right = Evidence(u["id"], Source.VISION, f"约定前缀 {prefix}", PLACEHOLDER_BBOX)
        if persist:
            issues.append(
                write_issue(
                    store,
                    version_id,
                    title=title,
                    left=left,
                    right=right,
                    category=CATEGORY_PREFIX,
                    left_value=printed,
                    right_value=str(prefix),
                    basis=f"图签编号前缀与约定 {prefix} 不符",
                    fixture_id="ISSUE-PREFIX-BAD",
                    extra=_title_diagnostic(u),
                )
            )
        else:
            issues.append({"title": title, "category": CATEGORY_PREFIX, "fixture_id": "ISSUE-PREFIX-BAD"})
    return issues
