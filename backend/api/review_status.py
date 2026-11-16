"""DC-15：问题列表、复核持久化与单元审查图薄层（不实现引擎/规则）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.importer.diagnostics import dwg_notice, is_dwg, preview_counts
from backend.store import Repository, StoreError
from backend.store.errors import ErrorCode
from backend.store.models import BBox, Evidence, Source
from backend.store.paths import safe_path
from .difference_text import comparison_basis, describe_difference

REVIEW_STATUSES = ("疑似", "已确认", "排除", "需补充资料")
# 按钮文案 → 入库状态（W6-Q1=B）
BUTTON_TO_STATUS = {
    "确认": "已确认",
    "已确认": "已确认",
    "排除": "排除",
    "需补充资料": "需补充资料",
    "疑似": "疑似",
}


def _unit_label(unit: dict | None) -> str:
    if not unit:
        return ""
    number = (unit.get("drawing_number") or "").strip()
    name = (unit.get("name") or "").strip()
    if number and name:
        return f"{number} {name}"
    return number or name or unit.get("id", "")


def _bbox_dict(prefix: str, row: dict) -> dict:
    return {
        "x0": float(row[f"{prefix}x0"]),
        "y0": float(row[f"{prefix}y0"]),
        "x1": float(row[f"{prefix}x1"]),
        "y1": float(row[f"{prefix}y1"]),
    }


def _bbox_out_of_range(box: dict) -> bool:
    values = (box["x0"], box["y0"], box["x1"], box["y1"])
    if any(not (0.0 <= v <= 1.0) for v in values):
        return True
    return box["x0"] > box["x1"] or box["y0"] > box["y1"]


def _image_relative(unit: dict | None) -> str | None:
    if not unit:
        return None
    meta = unit.get("metadata") or {}
    return meta.get("review_relative_path") or meta.get("preview_relative_path") or meta.get("page_relative_path")


def _proof(unit, evidence, *, value=None, fallback=""):
    box = evidence.get("bbox") or {}
    anchored = (len(box) == 4 and not _bbox_out_of_range(box)
                and box["x0"] < box["x1"] and box["y0"] < box["y1"])
    return {"unit_id": unit["id"] if unit else None,
            "drawing_number": (unit or {}).get("drawing_number", ""),
            "drawing_name": (unit or {}).get("name", ""),
            "sheet": _unit_label(unit) or fallback, "text": evidence.get("text") or "",
            "value": value if value is not None else (evidence.get("text") or ""),
            "bbox": box if anchored else None,
            "image_url": f"/api/units/{unit['id']}/image" if unit and _image_relative(unit) else None}


def serialize_unit_declarations(declarations, units):
    unique = {}
    for row in declarations or []:
        key = (row["unit_id"], row["category"], row["unit"], tuple(sorted(row["bbox"].items())))
        unique[key] = {**_proof(units.get(row["unit_id"]), row, value=row["unit"]),
                       "category": row["category"], "unit": row["unit"], "volume": row["volume"],
                       "source": row["source"], "ordinal": row["ordinal"]}
    return list(unique.values())


def serialize_issue(row: dict, units: dict[str, dict]) -> dict:
    left = units.get(row["left_unit_id"])
    right = units.get(row["right_unit_id"])
    left_box = _bbox_dict("left_", row)
    right_box = _bbox_dict("right_", row)
    sheets = []
    for unit_id in (row["left_unit_id"], row["right_unit_id"]):
        label = _unit_label(units.get(unit_id))
        if label and label not in sheets:
            sheets.append(label)
    location = (row.get("left_text") or row.get("right_text") or "").strip() or "不一致位置"
    description = ""
    rule = row.get("rule_result") or {}
    if isinstance(rule, dict):
        description = str(rule.get("summary") or rule.get("message") or "").strip()
    if not description:
        left_t = (row.get("left_text") or "").strip()
        right_t = (row.get("right_text") or "").strip()
        if left_t and right_t and left_t != right_t:
            description = f"「{left_t}」与「{right_t}」不一致。"
        else:
            description = left_t or right_t or "图纸之间存在不一致。"
    if rule.get("review_conditions_changed_after_confirmation"):
        description += " 审查条件已修改，请重新核对"
    elif rule.get("evidence_changed_after_confirmation"):
        description += " 标注已修改，请重新核对"
    review = row.get("review_opinion") or {}
    if review.get("verdict") == "disagree" and "两个模型意见不一致" not in description:
        description += " 两个模型意见不一致，请重点核对"
    category = rule.get("category") or "其他差异"
    category = "目录与图签" if category == "目录与图签不符" else category
    missing_target = row.get("right_text") == "（不存在）" or rule.get("fixture_id") == "ISSUE-CAT-MISSING"
    left_proof = _proof(left, {"text": row.get("left_text"), "bbox": left_box}, value=rule.get("left_value"))
    right_proof = _proof(None if missing_target else right,
                        {"text": row.get("right_text"), "bbox": None if missing_target else right_box},
                        value=rule.get("right_value"), fallback="引用目标或图纸缺失")
    basis = comparison_basis(category, rule)
    reason = describe_difference(category, rule, basis)
    assert reason != basis
    return {
        "review_opinion": review,
        "category": category,
        "finding": {"label": rule.get("object_label") or (rule.get("catalog_name_evidence") or {}).get("text") or category,
                    "basis": basis, "reason": reason,
                    "left": left_proof, "right": right_proof,
                    "unit_declarations": serialize_unit_declarations(rule.get("unit_declarations"), units)},
        "id": row["id"],
        "version_id": row["version_id"],
        "title": row["title"],
        "review_status": row["review_status"],
        "review_note": row.get("review_note") or "",
        "comparison_tag": row.get("comparison_tag"),
        "diff": row.get("comparison_tag") or "",
        "location": location,
        "description": description,
        "sheets": sheets,
        "main_unit_id": row["left_unit_id"],
        "related_unit_id": row["right_unit_id"] if row["right_unit_id"] != row["left_unit_id"] else None,
        "main_label": _unit_label(left),
        "related_label": _unit_label(right) if row["right_unit_id"] != row["left_unit_id"] else "",
        "main_bbox": left_box,
        "related_bbox": right_box,
        "main_bbox_out_of_range": _bbox_out_of_range(left_box),
        "related_bbox_out_of_range": _bbox_out_of_range(right_box),
        "main_image_url": f"/api/units/{row['left_unit_id']}/image",
        "related_image_url": (
            f"/api/units/{row['right_unit_id']}/image"
            if row["right_unit_id"] != row["left_unit_id"] else None
        ),
    }


def sort_issues(items: list[dict]) -> list[dict]:
    def key(item: dict):
        excluded = 1 if item["review_status"] == "排除" or item.get("comparison_tag") == "已解决" else 0
        main = item.get("main_label") or ""
        return (excluded, main, item.get("id") or "")

    return sorted(items, key=key)


def _comparison_evidence_gap(repo, units, finding):
    left, right = finding["left"], finding["right"]
    if not right["unit_id"]:
        return "缺少第二张原图，无法标出两处跨图证据"
    if left["unit_id"] == right["unit_id"]:
        return "两处记录来自同一张图纸，缺少跨图对照"
    for label, side in (("图纸一", left), ("图纸二", right)):
        if not side["image_url"] or not side["bbox"] or not side["text"].strip():
            return f"{label}缺少可定位的原文"
        try:
            image = safe_path(repo.root, _image_relative(units.get(side["unit_id"])))
            if image.is_file() and image.suffix.lower() in {".png", ".jpg", ".jpeg"}:
                continue
        except (StoreError, OSError):
            pass
        return f"{label}的原图预览不可用"
    return None


def list_issues(repo: Repository, version_id: str, *, summary_stub: dict | None = None) -> dict:
    repo.get("drawing_versions", version_id)
    units = {u["id"]: u for u in repo.list_records("drawing_units", version_id=version_id)}
    raw = repo.list_records("issues", version_id=version_id)
    items, evidence_gaps = [], []
    for row in raw:
        item = serialize_issue(row, units)
        finding = item["finding"]
        closed = item["review_status"] == "排除" or item["diff"] == "已解决"
        gap = None if closed else _comparison_evidence_gap(repo, units, finding)
        if gap:
            evidence_gaps.append({**finding, "id": item["id"], "category": item["category"],
                                  "outcome": "unverified", "reason": f"{gap}。原记录：{finding['reason']}"})
        else:
            items.append(item)
    items = sort_issues(items)
    from backend.pending.current import current_incomplete, current_pending
    pending = current_pending(repo, version_id)
    pending_open = sum(1 for p in pending if p.get("status") == "待确认")
    stub = summary_stub or {}
    tasks = repo.list_records("review_tasks", version_id=version_id)
    latest = tasks[-1] if tasks else {}
    checkpoints = latest.get("checkpoints") or {}
    selected_retry = (checkpoints.get("_meta") or {}).get("single_page_retry")
    phases = {"extract": "图纸识别", "semantic": "文字语义分析", "link": "图纸关联", "rules_a": "规则核查", "rules_b": "规则核查",
              "dual_review": "第二模型复核", "rules_reconciliation": "人工结论复核", "finalize": "结果汇总",
              "pending_location": "识读定位未核验"}
    incomplete_items = []
    facts = None
    from backend.extract.service import can_retry_visual
    from backend.references import _independent_title
    current_gaps = current_incomplete(repo, version_id, latest.get("incomplete", []))
    originals = None
    availability = preview_counts(units.values())
    for row in current_gaps:
        unit = units.get(row.get("unit_id"), {})
        original_id = row.get("original_id")
        reason = str(row.get("reason") or "未完成")
        source_name = ""
        source_notice = None
        if not row.get("unit_id") and row.get("category") == "unreadable":
            name = ""
            if original_id:
                if originals is None:
                    originals = {o["id"]: o["name"] for o in repo.list_records("original_files", version_id=version_id)}
                name = originals.get(original_id, "")
            elif row.get("step") == "extract":
                # Empty or corrupt uploads can be rejected before an original is archived.
                filename, _, rejected = reason.rpartition("：")
                if rejected in {"文件为空，读不了", "文件损坏，读不了"}:
                    name = filename
            if is_dwg(name):
                source_name = name
                source_notice = dwg_notice(reason, availability.get(original_id, (0, 0)))
        vision_incomplete = (row.get("step") == "extract" and row.get("category") == "incomplete"
                             and (unit.get("metadata") or {}).get("extract_status") == "incomplete")
        title_verified = retryable = False
        if vision_incomplete:
            if facts is None:
                facts = repo.list_records("facts", version_id=version_id)
            title_verified = _independent_title(unit, facts)
            retryable = selected_retry == unit["id"] or (
                not selected_retry and latest.get("status") == "已完成"
                and (checkpoints.get("extract") or {}).get("state") == "done" and can_retry_visual(repo.root, unit))
        incomplete_items.append({"unit_id": unit.get("id") or row.get("unit_id"), "drawing_number": unit.get("drawing_number", ""),
            "drawing_name": source_name or unit.get("name", "整套图纸"), "reason": reason,
            "phase": "导入图纸" if source_notice else "视觉识别未完成" if vision_incomplete else phases.get(row.get("step"), "审查"),
            "category": row.get("category", "incomplete"),
            "title_verified": title_verified, "retryable": retryable,
            **({"notice": source_notice} if source_notice else {}),
            **({"pending_ids": row["pending_ids"], "original_texts": row["original_texts"]}
               if row.get("step") == "pending_location" else {})})
    from backend.evidence_selection import selected_facts
    from backend.rules.engineering import run_engineering
    from backend.rules.unit_declaration import volume_units
    task_id = tasks[-1]["id"] if tasks else None
    _, selected = selected_facts(repo, version_id)
    declarations = volume_units(repo, version_id, units=units.values(), facts=selected)
    comparisons = run_engineering(repo, version_id, task_id=task_id, units=units.values(),
                                  persist=False, declarations=declarations)["comparisons"]
    from .comparison_results import comparison_results, comparison_summary, verification_boundary
    details, used = comparison_results(repo, version_id, units, comparisons, task_id=task_id, facts=selected,
                                       declarations=declarations)
    scope = comparison_summary(details + evidence_gaps, items)
    details = [{**row, "outcome": "unverified"} for row in details if row["outcome"] != "match"]
    for index, row in enumerate(details):
        row["id"] = str(index)
        if not row.get("basis") and row["category"] == "接线":
            row["basis"] = "按同一对接线端点核对" if row.get("domain") == "光纤" else "按同一信号码核对线号"
        if row.get("unit_declarations"):
            row["unit_declarations"] = serialize_unit_declarations(row["unit_declarations"], units)
        for side in ("left", "right"):
            unit = units.get(row[f"{side}_unit_id"])
            proof = _proof(unit, row.get(f"{side}_evidence") or {}, value=row.get(f"{side}_value"))
            if unit and row.get("shared_block_mark") and not proof["drawing_number"]:
                if originals is None:
                    originals = {o["id"]: o["name"] for o in repo.list_records("original_files", version_id=version_id)}
                if name := originals.get(unit["original_id"]):
                    proof["sheet"] = f"{name} · 第 {unit['ordinal']} 图"
            row[side] = proof
    return {
        "version_id": version_id,
        "items": items,
        "comparison_details": details + evidence_gaps,
        "comparison_summary": scope,
        "unit_declarations": serialize_unit_declarations([*declarations.evidence(), *used], units),
        "verification": verification_boundary(scope),
        "incomplete_items": incomplete_items,
        "outcome": ("partial" if any(row.get("step") == "pending_location" for row in current_gaps)
                    and stub.get("outcome") in (None, "completed") else stub.get("outcome")),
        "recognized_count": sum((unit.get("metadata") or {}).get("extract_status") == "ok" for unit in units.values()),
        "drawing_count": len(units),
        "pending_count": pending_open,
        "issue_count": len(items),
        "incomplete_count": max(int(stub.get("incomplete_count", 0)),
            sum(row.get("category", "incomplete") not in {"unreadable", "out_of_scope"} for row in current_gaps)
            + sum(p.get("status") != "已确认" for p in pending)),
        "unreadable_count": max(int(stub.get("unreadable_count", 0)),
            sum(row.get("category") == "unreadable" for row in current_gaps)),
        "out_of_scope_count": max(int(stub.get("out_of_scope_count", 0)),
            sum(row.get("category") == "out_of_scope" for row in current_gaps)),
        "cost_paused": bool(stub.get("cost_paused", False)),
        "revised": bool(stub.get("revised", False)),
    }


def patch_issue(repo: Repository, issue_id: str, body: dict) -> dict:
    if not isinstance(body, dict):
        raise StoreError(ErrorCode.INVALID_DATA, "请求体无效")
    raw_status = body.get("review_status")
    if not isinstance(raw_status, str):
        raise StoreError(ErrorCode.INVALID_DATA, "请选择复核结论")
    status = BUTTON_TO_STATUS.get(raw_status.strip(), raw_status.strip())
    if status not in REVIEW_STATUSES:
        raise StoreError(ErrorCode.INVALID_DATA, "复核状态无效")
    note = body.get("review_note", "")
    if note is None:
        note = ""
    if not isinstance(note, str) or len(note) > 2000:
        raise StoreError(ErrorCode.INVALID_DATA, "备注过长")
    existing = repo.get("issues", issue_id)
    # 再点当前态 → 回疑似（Q-13 可改回）
    if body.get("toggle") and existing["review_status"] == status and status != "疑似":
        status = "疑似"
    updated = repo.update_issue(issue_id, review_status=status, review_note=note)
    from backend.rules.registry import acknowledge_manual_recheck
    acknowledge_manual_recheck(repo, issue_id)
    updated = repo.get("issues", issue_id)
    units = {u["id"]: u for u in repo.list_records("drawing_units", version_id=updated["version_id"])}
    return serialize_issue(updated, units)


def resolve_unit_image(repo: Repository, unit_id: str) -> Path:
    unit = repo.get("drawing_units", unit_id)
    relative = _image_relative(unit)
    if not relative:
        raise StoreError(ErrorCode.NOT_FOUND, "审查图不存在")
    path = safe_path(repo.root, relative)
    if not path.is_file():
        raise StoreError(ErrorCode.NOT_FOUND, "审查图文件缺失")
    return path


def seed_mock_issues(repo: Repository, version_id: str, *, force: bool = False) -> dict:
    """无 DC-11 时注入静态夹具问题，供结果页联调。不改 fixtures/ 目录。"""
    repo.get("drawing_versions", version_id)
    existing = repo.list_records("issues", version_id=version_id)
    if existing and not force:
        return list_issues(repo, version_id)
    units = repo.list_records("drawing_units", version_id=version_id)
    if len(units) < 1:
        raise StoreError(ErrorCode.INVALID_DATA, "没有可挂问题的图纸单元")
    if existing and force:
        # 本波不提供删库 API；force 仅在空列表场景使用。已有问题时直接返回。
        return list_issues(repo, version_id)
    primary = units[0]
    secondary = units[1] if len(units) > 1 else units[0]
    samples = [
        {
            "title": "箱变基础尺寸不一致",
            "left": Evidence(primary["id"], Source.VISION, "2400×1800mm", BBox(0.27, 0.38, 0.46, 0.50)),
            "right": Evidence(secondary["id"], Source.VISION, "2500×1800mm", BBox(0.56, 0.43, 0.86, 0.54)),
            "rule": {"summary": "主图标注 2400×1800mm，对照图标注 2500×1800mm，基础长度不一致。",
                     "category": "尺寸", "basis": "按同一箱变基础的尺寸配对。", "max_abs_diff": "100", "code": "MOCK_DIM"},
        },
        {
            "title": "支架基础标高不一致",
            "left": Evidence(primary["id"], Source.VISION, "±0.000=4.350m", BBox(0.17, 0.35, 0.52, 0.43)),
            "right": Evidence(secondary["id"], Source.VISION, "4.300m", BBox(0.13, 0.43, 0.42, 0.52)),
            "rule": {"summary": "主图标高 ±0.000=4.350m，对照图为 4.300m，基准标高不一致。",
                     "category": "标高", "basis": "按同一支架基础的基准标高配对。", "abs_diff": "0.050", "code": "MOCK_ELEV"},
        },
        {
            "title": "设备编号格式不一致",
            "left": Evidence(primary["id"], Source.VISION, "XB-03", BBox(0.25, 0.29, 0.58, 0.37)),
            "right": Evidence(secondary["id"], Source.VISION, "XB-3", BBox(0.76, 0.86, 0.95, 0.95)),
            "rule": {"summary": "主图编号 XB-03，对照图写为 XB-3，编号格式不一致。", "category": "编号写法",
                     "basis": "按同一设备的位号配对。", "left_value": "XB-03", "right_value": "XB-3", "code": "MOCK_ID"},
        },
    ]
    # 可选：一条出界坐标（仍能入库？ store BBox 校验禁止出界）——改用前端提示桩字段
    # 用合法坐标即可；前端另测 404 图
    for sample in samples:
        repo.create_issue(
            version_id,
            sample["title"],
            sample["left"],
            sample["right"],
            rule_result=sample["rule"],
            primary_opinion={"mock": True},
            review_opinion=None,
        )
    return list_issues(repo, version_id)


def summarize_from_project_hint(project: dict | None) -> dict[str, Any]:
    """从前端导入结果推导读不了计数等桩字段。"""
    if not project:
        return {}
    unreadable = project.get("unreadable") or []
    return {
        "unreadable_count": len(unreadable),
        "incomplete_count": 0,
        "out_of_scope_count": 0,
        "cost_paused": False,
        "revised": False,
    }
