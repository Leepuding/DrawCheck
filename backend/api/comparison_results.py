"""Read-only projection of cross-sheet checks for the review page."""
from collections import Counter

from backend.evidence_selection import selected_facts
from backend.rules.catalog_titleblock import local_catalog_checks
from backend.rules.dimension import run_dimension
from backend.rules.elevation import run_elevation
from backend.rules.rules_b import RulesBService
from backend.rules.design_parameters import design_parameter_candidates


def comparison_results(repo, version_id, units, engineering, *, task_id=None, facts=None, declarations=None):
    if facts is None:
        _, facts = selected_facts(repo, version_id)
    catalogs = [unit for unit in units.values() if (unit.get("metadata") or {}).get("is_catalog_sheet")
                and not (unit.get("metadata") or {}).get("out_of_scope")]
    _, catalog = local_catalog_checks(repo, version_id, units.values(), catalogs)
    rows = (
        list(engineering) + catalog
        + run_dimension(repo, version_id, task_id=task_id, persist=False, comparison_facts=facts,
                        declarations=declarations)["comparisons"]
        + run_elevation(repo, version_id, task_id=task_id, persist=False, comparison_facts=facts,
                        declarations=declarations)["comparisons"]
        + RulesBService(repo).run(version_id, comparison_facts=facts).comparisons
        + design_parameter_candidates(units, facts)
    )
    # Internal coverage projection only. Matches must never be returned as
    # user-facing rows; retain them here solely to establish verified scope.
    unique = {}
    for row in rows:
        if row["outcome"] == "mismatch" or row["left_unit_id"] == row["right_unit_id"]:
            continue
        if row.get("domain") == "电缆编号":
            row = {**row, "category": "电缆编号"}
        key = (row["category"], tuple(sorted(
            (row[f"{side}_unit_id"], row.get(f"{side}_value", evidence["text"]),
             tuple(evidence["bbox"].get(k) for k in ("x0", "y0", "x1", "y1")))
            for side in ("left", "right") if (evidence := row.get(f"{side}_evidence"))
        )))
        existing = unique.get(key)
        if existing is None or row["outcome"] == "unverified":
            unique[key] = row
    used_declarations = [e for row in rows if row["outcome"] == "match" for e in row.get("unit_declarations", [])]
    return list(unique.values()), used_declarations


def comparison_summary(rows, findings):
    counts = Counter((row["category"], "verified" if row["outcome"] == "match" else "unverified") for row in rows)
    for finding in findings:
        if finding["review_status"] != "排除" and finding["diff"] != "已解决":
            counts[finding["category"], "difference"] += 1
    categories = ("尺寸", "标高", "材料规格", "数量与材料表", "接线", "电缆编号", "设备位号", "编号写法", "目录与图签")
    return [{"category": category,
             "difference": counts[category, "difference"],
             "unverified": counts[category, "unverified"],
             "verified": bool(counts[category, "verified"] or counts[category, "difference"])}
            for category in dict.fromkeys((*categories, *(category for category, _ in counts)))]


def verification_boundary(summary):
    effective = any(row["verified"] for row in summary)
    return {"has_effective_comparison": effective,
            "zero_difference_message": "已核验范围内未发现差异" if effective else "尚未形成有效对照",
            "scope_note": "仅覆盖已建立可信对应关系的内容，未核验部分不能据此判断一致。"}
