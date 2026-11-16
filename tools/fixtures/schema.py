"""answers.json 结构校验（等价于 JSON Schema）。"""
from __future__ import annotations

from typing import Any

from backend.store.models import BBox

CATEGORIES = (
    "目录张数",
    "图签编号前缀",
    "尺寸",
    "标高",
    "编号写法",
    "材料规格",
    "数量与材料表",
    "详图索引断链",
    "同名不同对象",
    "目录与图签不符",
)

EXPECTATIONS = (
    "应报",
    "不应报",
    "待确认",
    "未核验",
    "不下结论",
)

REVISION_TAGS = ("仍存在", "已解决", "新出现", None)


def _require(cond: bool, msg: str):
    if not cond:
        raise ValueError(msg)


def validate_answers(data: dict[str, Any]) -> None:
    _require(isinstance(data, dict), "answers 根须为对象")
    _require(data.get("kind") == "synthetic-dev-answers", "kind 须为 synthetic-dev-answers")
    _require(data.get("is_golden_sample") is False, "须标明非金样本")
    catalog = data.get("catalog") or {}
    numbers = {row["drawing_number"] for row in catalog.get("A", [])} | {
        row["drawing_number"] for row in catalog.get("B", [])
    }
    _require(numbers, "catalog 不能为空")

    for issue in data.get("issues", []):
        _require(issue.get("category") in CATEGORIES, f"未知类别: {issue.get('category')}")
        _require(issue.get("expectation") in EXPECTATIONS, f"未知期望: {issue.get('expectation')}")
        for key in ("side_a", "side_b"):
            side = issue.get(key)
            if side is None:
                continue
            dn = side.get("drawing_number")
            if dn is not None:
                _require(dn in numbers or dn in catalog.get("listed_missing", []),
                         f"问题引用未知图号: {dn}")
            bbox = side.get("bbox")
            if bbox is not None:
                BBox(**bbox)  # 0～1、左上、x0<=x1
            if "numeric_value" in side and side["numeric_value"] is not None:
                _require(isinstance(side["numeric_value"], str), "numeric_value 须为十进制字符串")
        tol = issue.get("tolerance")
        if tol:
            _require(tol.get("config_key") in ("dimension_tolerance_mm", "elevation_tolerance_m"),
                     f"未知容差键: {tol.get('config_key')}")
            _require(tol.get("boundary") in ("equal", "over", None, "missing_unit"),
                     f"未知 boundary: {tol.get('boundary')}")
            # 裁定：等于容差不报，仅超过才报
            if tol.get("boundary") == "equal":
                _require(issue["expectation"] == "不应报", "等于容差须为不应报")
            if tol.get("boundary") == "over":
                _require(issue["expectation"] == "应报", "超过容差须为应报")
            if tol.get("boundary") == "missing_unit":
                _require(issue["expectation"] == "未核验", "缺单位保持未核验")

    rev = data.get("revision_summary") or {}
    for key in ("仍存在", "已解决", "新出现"):
        _require(isinstance(rev.get(key), int) and rev[key] >= 1, f"修订汇总 {key} 须 ≥1")
    unchanged = data.get("unchanged_drawings") or []
    _require(len(unchanged) >= 1, "至少 1 张未改动图")
