"""解析器协议：本单不包含 PDF/DWG 解析或转换实现。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, Sequence, runtime_checkable

from backend.store.models import BBox, Source


@dataclass(frozen=True)
class ParseInput:
    original_id: str
    readonly_path: Path
    sha256: str
    cache_dir: Path
    format: Literal["pdf", "dwg"]


@dataclass(frozen=True)
class ParsedText:
    text: str
    source: Source
    bbox: BBox


@dataclass(frozen=True)
class ParsedUnit:
    kind: Literal["pdf_page", "dwg_frame"]
    ordinal: int
    name: str
    drawing_number: str
    texts: Sequence[ParsedText]
    # DWG 图框裁切后的单元坐标，渲染缓存只能放入调用方给定的 cache_dir。
    preview_relative_path: str | None = None
    native_measurements: tuple[dict, ...] = field(default=(), kw_only=True)
    native_signals: tuple[dict, ...] = field(default=(), kw_only=True)
    native_connections: tuple[dict, ...] = field(default=(), kw_only=True)


@dataclass(frozen=True)
class ParseResult:
    units: Sequence[ParsedUnit]
    incomplete: Sequence[str]


@runtime_checkable
class Parser(Protocol):
    """实现方仅可读取原图，衍生文件写 cache_dir，所有文字框为单元内 0～1。"""

    def parse(self, request: ParseInput) -> ParseResult:
        ...
