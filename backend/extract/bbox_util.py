"""坐标框裁剪到 [0,1]。"""
from __future__ import annotations

import logging
import math
from typing import Any

from .errors import CODE_BAD_FORMAT, ExtractError

logger = logging.getLogger("drawcheck.extract.bbox")


def clamp_bbox(raw: dict[str, Any] | None) -> tuple[dict[str, float], bool, list[str]]:
    """裁到 [0,1] 并保证 x0<=x1、y0<=y1。返回 (bbox, clamped?, notes)。"""
    notes: list[str] = []
    if not isinstance(raw, dict):
        notes.append("bbox 缺失，使用全图框")
        return {"x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0}, True, notes

    clamped = False
    values: dict[str, float] = {}
    for key in ("x0", "y0", "x1", "y1"):
        try:
            v = float(raw.get(key))
            if not math.isfinite(v):
                # Non-finite coordinates cannot be clamped into an evidence
                # location. Reject this page/cache instead of inventing one.
                raise ExtractError(CODE_BAD_FORMAT, f"bbox.{key} 须为有限坐标")
        except (TypeError, ValueError, OverflowError):
            v = 0.0 if key in ("x0", "y0") else 1.0
            notes.append(f"{key} 非法，置默认")
            clamped = True
        if v < 0.0:
            notes.append(f"{key}={v} <0 → 0")
            v = 0.0
            clamped = True
        elif v > 1.0:
            notes.append(f"{key}={v} >1 → 1")
            v = 1.0
            clamped = True
        values[key] = v

    if values["x0"] > values["x1"]:
        notes.append(f"x0>x1 交换 ({values['x0']},{values['x1']})")
        values["x0"], values["x1"] = values["x1"], values["x0"]
        clamped = True
    if values["y0"] > values["y1"]:
        notes.append(f"y0>y1 交换 ({values['y0']},{values['y1']})")
        values["y0"], values["y1"] = values["y1"], values["y0"]
        clamped = True

    if clamped:
        logger.info("bbox 已裁剪: %s", "; ".join(notes))
    return values, clamped, notes
