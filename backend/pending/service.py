"""待你确认：列表、确认/无法确定、写回用户确认事实、重算钩子（无 DC-11 则 skip）。"""
from __future__ import annotations

from contextlib import nullcontext
from typing import Any
import math

from backend.store import Repository, StoreError
from backend.store.errors import ErrorCode
from backend.store.models import BBox, Evidence, Source
from .current import (INVALID_LOCATION_REASON, current_pending, invalid_location_incomplete,
                      invalid_open_location, valid_pending_bbox, unlocated_pending_ids)

OPEN = "待确认"
CONFIRMED = "已确认"
UNSURE = "无法确定"
STATUSES = (OPEN, CONFIRMED, UNSURE)
MAX_TEXT = 80


def _unit_label(unit: dict | None) -> str:
    if not unit:
        return ""
    number = (unit.get("drawing_number") or "").strip()
    name = (unit.get("name") or "").strip()
    if number and name:
        return f"{number} {name}"
    return number or name or unit.get("id", "")


def _bbox_out_of_range(x0: float, y0: float, x1: float, y1: float) -> bool:
    return not valid_pending_bbox(dict(x0=x0, y0=y0, x1=x1, y1=y1))


def _title(row: dict) -> str:
    text = (row.get("text") or "").strip()
    reason = (row.get("reason") or "").strip() or "待确认"
    if target := row.get("reference_target"):
        return f"{reason}：{text}（同卷册目标 {target}）"
    if text:
        return f"{reason}「{text}」" if "看不清" in reason or "不一致" in reason else f"{reason}：{text}"
    return reason


def _location(row: dict) -> str:
    reason = (row.get("reason") or "").strip()
    if "看不清" in reason:
        return "看不清标注处"
    if "不一致" in reason:
        return "两模型读数不一致处"
    return reason or "待确认位置"


def serialize_pending(row: dict, units: dict[str, dict]) -> dict:
    unit = units.get(row["unit_id"])
    box = {}
    for key in ("x0", "y0", "x1", "y1"):
        try:
            value = float(row.get(key))
            box[key] = value if math.isfinite(value) else None
        except (TypeError, ValueError, OverflowError):
            box[key] = None
    status = row["status"]
    from backend.api.review_status import serialize_unit_declarations
    return {
        "id": row["id"],
        "version_id": row["version_id"],
        "unit_id": row["unit_id"],
        "title": _title(row),
        "location": _location(row),
        "reason": row.get("reason") or "",
        **({"reference_target": row["reference_target"]} if row.get("reference_target") else {}),
        "text": row.get("text") or "",
        "status": status,
        "confirmed_text": row.get("confirmed_text"),
        "source": row.get("source") or "",
        "bbox": box,
        "bbox_out_of_range": not unit or not valid_pending_bbox(row),
        "main_label": _unit_label(unit),
        "main_image_url": f"/api/units/{row['unit_id']}/image",
        "participates_in_comparison": status == CONFIRMED,
        "counts_incomplete": status in (OPEN, UNSURE),
        "processed": status != OPEN,
        **({"unit_declarations": serialize_unit_declarations(row["unit_declarations"], units)}
           if row.get("unit_declarations") else {}),
    }


def _model_call_count(repo: Repository, version_id: str) -> int:
    return len(repo.list_records("model_calls", version_id=version_id))


def recalculate_affected(repo: Repository, version_id: str, pending: dict) -> dict[str, Any]:
    """填值后触发受影响规则重算。无 DC-11 / registry 则 skip；禁止新增模型调用。"""
    before = _model_call_count(repo, version_id)
    result: dict[str, Any] = {
        "invoked": False,
        "skipped": True,
        "reason": "rules_unavailable",
        "model_calls_before": before,
        "model_calls_after": before,
        "model_calls_delta": 0,
    }
    try:
        from backend import rules as rules_pkg  # type: ignore
    except ImportError:
        return result
    registry = getattr(rules_pkg, "registry", None)
    fn = None
    if registry is not None:
        fn = getattr(registry, "recalculate_for_pending", None) or getattr(
            registry, "recalculate_affected", None
        )
    if fn is None:
        fn = getattr(rules_pkg, "recalculate_for_pending", None)
    if not callable(fn):
        return result
    detail = fn(repo, version_id=version_id, pending=pending)
    after = _model_call_count(repo, version_id)
    result.update(
        {
            "invoked": True,
            "skipped": False,
            "reason": None,
            "detail": detail,
            "model_calls_after": after,
            "model_calls_delta": after - before,
        }
    )
    return result


def list_pending(repo: Repository, version_id: str) -> dict:
    repo.get("drawing_versions", version_id)
    units = {u["id"]: u for u in repo.list_records("drawing_units", version_id=version_id)}
    raw = current_pending(repo, version_id)
    items = [serialize_pending(row, units) for row in raw]
    # 待确认在前，已处理在后；同组按 id
    items.sort(key=lambda item: (1 if item["processed"] else 0, item["main_label"], item["id"]))
    pending_count = sum(1 for item in items if item["status"] == OPEN)
    incomplete_pending = sum(1 for item in items if item["counts_incomplete"])
    location_gaps = invalid_location_incomplete(repo, version_id, units=units)
    from backend.local_reading import local_reading_recoveries
    recoveries = local_reading_recoveries(repo, version_id, units=units)
    from .title_source import resolved_catalog_questions
    return {
        "version_id": version_id,
        "items": items,
        "pending_count": pending_count,
        "incomplete_pending_count": incomplete_pending,
        "location_gap_count": len(location_gaps),
        "recovered_readings": list(recoveries.values()),
        "resolved_title_sources": list(resolved_catalog_questions(repo, version_id).values()),
        "incomplete_items": location_gaps,
    }


def confirm_pending_item(repo: Repository, pending_id: str, body: dict) -> dict:
    """确认或无法确定；可对已处理项再次提交（W7-Q1=A）。写回用户确认事实；触发重算钩子。"""
    if not isinstance(body, dict):
        raise StoreError(ErrorCode.INVALID_DATA, "请求体无效")
    status = body.get("status")
    if status not in STATUSES or status == OPEN:
        raise StoreError(ErrorCode.INVALID_DATA, "请选择已确认或无法确定")
    text = body.get("confirmed_text")
    if text is None:
        text = ""
    if not isinstance(text, str):
        raise StoreError(ErrorCode.INVALID_DATA, "正确内容无效")
    text = text.strip()
    if len(text) > MAX_TEXT:
        raise StoreError(ErrorCode.INVALID_DATA, f"正确内容不超过 {MAX_TEXT} 字")
    if status == CONFIRMED and not text:
        raise StoreError(ErrorCode.INVALID_DATA, "确认时须填写正确内容")
    if status == UNSURE:
        text = None

    existing = repo.get("pending_confirmations", pending_id)
    version_id = existing["version_id"]
    units = {u["id"]: u for u in repo.list_records("drawing_units", version_id=version_id)}
    unlocated = unlocated_pending_ids(repo, version_id, rows=[existing], units=units)
    if invalid_open_location(existing, units, unlocated_ids=unlocated):
        raise StoreError(ErrorCode.INVALID_DATA, f"{INVALID_LOCATION_REASON}，不能确认此机器记录")
    from backend.local_reading import corroborated_prose_questions

    if corroborated_prose_questions(repo, version_id, rows=[existing], units=units):
        raise StoreError(ErrorCode.INVALID_DATA, "该条原文已有本地识读依据，无需重复确认，请刷新列表")
    from backend.evidence_selection import short_reference_questions
    from backend.references import AMBIGUOUS_REFERENCE_REASON, short_reference_answer

    stored = (repo.list_records("facts", version_id=version_id)
              if existing["reason"] == AMBIGUOUS_REFERENCE_REASON else [])
    short_reference = short_reference_questions(units, stored, [existing]).get(pending_id)
    if short_reference and status == CONFIRMED and not short_reference_answer(text):
        raise StoreError(ErrorCode.INVALID_DATA, "请填写明确的完整图号，无法确定时请选择「无法确定」")
    model_before = _model_call_count(repo, version_id)

    with repo._transaction() if short_reference else nullcontext():
        updated = repo.confirm_pending(
            pending_id,
            status=status,
            confirmed_text=text if status == CONFIRMED else None,
        )

        fact = None
        if status == CONFIRMED:
            evidence = Evidence(
                updated["unit_id"],
                Source.USER,
                text or "",
                BBox(float(updated["x0"]), float(updated["y0"]), float(updated["x1"]), float(updated["y1"])),
            )
            if short_reference:
                columns = evidence.columns()
                fact = next((row for row in repo.list_records("facts", version_id=version_id)
                             if all(row.get(key) == value for key, value in columns.items())), None)
            fact = fact or repo.add_fact(version_id, evidence)

    recalc = recalculate_affected(repo, version_id, updated)
    if short_reference:
        from backend.linking import LinkingService

        LinkingService(repo).link_version(version_id, reference_fact_id=short_reference["id"])
    model_after = _model_call_count(repo, version_id)
    units = {u["id"]: u for u in repo.list_records("drawing_units", version_id=version_id)}
    item = serialize_pending(updated, units)
    bundle = list_pending(repo, version_id)
    return {
        "item": item,
        "items": bundle["items"],
        "pending_count": bundle["pending_count"],
        "incomplete_pending_count": bundle["incomplete_pending_count"],
        "fact_id": fact["id"] if fact else None,
        "fact_source": Source.USER.value if fact else None,
        "recalc": recalc,
        "model_calls_before": model_before,
        "model_calls_after": model_after,
        "model_calls_delta": model_after - model_before,
    }


def seed_mock_pending(repo: Repository, version_id: str, *, force: bool = False) -> dict:
    """无识别 pending 时注入 DC-05 风格模糊待确认，供界面联调。"""
    repo.get("drawing_versions", version_id)
    existing = repo.list_records("pending_confirmations", version_id=version_id)
    if existing and not force:
        return list_pending(repo, version_id)
    units = repo.list_records("drawing_units", version_id=version_id)
    if not units:
        raise StoreError(ErrorCode.INVALID_DATA, "没有可挂待确认的图纸单元")
    primary = units[0]
    samples = [
        {
            "text": "2?00",
            "reason": "看不清",
            "bbox": BBox(0.65, 0.47, 0.76, 0.57),
        },
        {
            "text": "CAM-0?",
            "reason": "看不清",
            "bbox": BBox(0.36, 0.41, 0.53, 0.51),
        },
    ]
    for sample in samples:
        evidence = Evidence(primary["id"], Source.VISION, sample["text"], sample["bbox"])
        repo.create_pending(version_id, evidence, sample["reason"])
    return list_pending(repo, version_id)
