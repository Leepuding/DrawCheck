"""数据边界值：坐标原点为图纸单元左上角，所有坐标已归一化。"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from enum import Enum

from .errors import ErrorCode, StoreError


class Source(str, Enum):
    VISION = "看图识别"
    PDF_TEXT = "PDF文字"
    CAD_TEXT = "CAD文字"
    USER = "用户确认"


@dataclass(frozen=True)
class BBox:
    x0: float
    y0: float
    x1: float
    y1: float

    def __post_init__(self):
        values = (self.x0, self.y0, self.x1, self.y1)
        if (any(isinstance(v, bool) or not isinstance(v, (int, float))
                or not math.isfinite(v) or not 0 <= v <= 1 for v in values)
                or self.x0 > self.x1 or self.y0 > self.y1):
            raise StoreError(ErrorCode.INVALID_DATA, "坐标框必须在 0～1 内且起点不大于终点")


@dataclass(frozen=True)
class Evidence:
    unit_id: str
    source: Source
    text: str
    bbox: BBox

    def __post_init__(self):
        if not isinstance(self.bbox, BBox) or self.source not in set(Source):
            raise StoreError(ErrorCode.INVALID_DATA, "证据必须包含有效来源与坐标框")

    def columns(self, prefix: str = "") -> dict:
        return {f"{prefix}unit_id": self.unit_id, f"{prefix}source": str(Source(self.source).value),
                f"{prefix}text": self.text,
                **{f"{prefix}{key}": value for key, value in asdict(self.bbox).items()}}


def default_settings() -> dict:
    """Q-01/Q-02 默认值；Q-03 只预留非密钥配置。"""
    return {"dimension_tolerance_mm": 1, "elevation_tolerance_m": 0.005,
            "monthly_limit_yuan": None, "billing_period": "calendar_month",
            "currency": "CNY", "cost_warning_ratio": 0.8,
            "primary_model": None, "review_model": None}
