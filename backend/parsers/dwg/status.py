"""DWG 文件级状态；与通用解析协议分开，避免影响并行的 PDF 管线。"""
from dataclasses import dataclass, field
from enum import Enum

from backend.parsers.base import ParseResult


class DwgFileStatus(str, Enum):
    SUCCESS = "成功"
    PARTIAL = "部分成功"
    UNREADABLE = "读不了"


COMPONENT_UNAVAILABLE = "DWG 解析组件不可用"


@dataclass(frozen=True)
class DwgFileResult:
    status: DwgFileStatus
    reason: str
    result: ParseResult
    statistics: dict = field(default_factory=dict)
    sha256_before: str | None = None
    sha256_after: str | None = None

    @property
    def message(self) -> str:
        return f"{self.status.value}：{self.reason}"
