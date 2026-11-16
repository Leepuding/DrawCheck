"""只读 PDF 输入，按页生成独立 PDF、72 dpi 预览及 150 dpi 审查图。"""
from __future__ import annotations

import math
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PyPdfError

from backend.parsers.base import ParseInput, ParseResult, ParsedUnit
from backend.store.errors import ErrorCode, StoreError
from backend.store.originals import sha256_file
from backend.store.paths import safe_path


class PDFError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class PDFPageFailure:
    page: int
    code: str
    reason: str


@dataclass(frozen=True)
class PDFUnit(ParsedUnit):
    review_relative_path: str | None = None
    page_relative_path: str | None = None
    width_points: float = 0
    height_points: float = 0


@dataclass(frozen=True)
class PDFParseResult(ParseResult):
    failures: tuple[PDFPageFailure, ...] = ()


class PDFParser:
    def __init__(self, *, page_timeout: float = 60, info_timeout: float = 60,
                 max_dimension: int = 8192, preview_dpi: int = 72, review_dpi: int = 150,
                 pdftoppm: str = "pdftoppm", pdfinfo: str = "pdfinfo"):
        for value in (page_timeout, info_timeout, max_dimension, preview_dpi, review_dpi):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("超时、像素上限及分辨率必须为正有限数值")
        if any(not isinstance(value, int) for value in (max_dimension, preview_dpi, review_dpi)):
            raise ValueError("像素上限及分辨率必须为整数")
        self.page_timeout = page_timeout
        self.info_timeout = info_timeout
        self.max_dimension = max_dimension
        self.preview_dpi = preview_dpi
        self.review_dpi = review_dpi
        self.pdftoppm = pdftoppm
        self.pdfinfo = pdfinfo

    @staticmethod
    def _run(command: list[str], timeout: float):
        try:
            # run 超时会杀死并回收直接子进程；不经过 shell，不弹出 Windows 命令行窗口。
            subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=True, timeout=max(timeout, .001),
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            raise PDFError("RENDER_TIMEOUT", "渲染超时") from exc
        except FileNotFoundError as exc:
            raise PDFError("POPPLER_UNAVAILABLE", "缺少 PDF 渲染工具，读不了") from exc
        except subprocess.CalledProcessError as exc:
            raise PDFError("RENDER_FAILED", "页面渲染失败，读不了") from exc

    def parse(self, request: ParseInput) -> PDFParseResult:
        if request.format != "pdf":
            raise PDFError("UNSUPPORTED_FORMAT", "不支持的格式")
        source = request.readonly_path.resolve()
        cache = request.cache_dir
        # 拒绝缓存经过链接或落在原图目录内，避免衍生物混进只读 originals。
        if any(path.is_symlink() for path in (cache, *cache.parents)):
            raise StoreError(ErrorCode.INVALID_PATH, "缓存目录不能经过符号链接")
        cache = cache.resolve()
        if cache.is_relative_to(source.parent) or source.is_relative_to(cache):
            raise StoreError(ErrorCode.INVALID_PATH, "缓存目录必须与原图目录分开")
        if sha256_file(source) != request.sha256:
            raise StoreError(ErrorCode.ORIGINAL_CHANGED, "原图哈希已改变")
        try:
            return self._parse(request, source, cache)
        finally:
            if sha256_file(source) != request.sha256:
                raise StoreError(ErrorCode.ORIGINAL_CHANGED, "解析期间原图哈希已改变")

    def _parse(self, request: ParseInput, source: Path, cache: Path) -> PDFParseResult:
        units, failures = [], []
        with source.open("rb") as stream:
            try:
                reader = PdfReader(stream, strict=False)
                if reader.is_encrypted:
                    raise PDFError("PDF_ENCRYPTED", "PDF 有密码，读不了")
                count = len(reader.pages)
                if count == 0:
                    raise PDFError("PDF_EMPTY", "PDF 没有页面，读不了")
            except (PyPdfError, ValueError, TypeError, KeyError, IndexError, RecursionError) as exc:
                raise PDFError("PDF_CORRUPT", "文件损坏，读不了") from exc
            try:
                self._run([self.pdfinfo, str(source)], self.info_timeout)
            except PDFError as exc:
                if exc.code == "RENDER_FAILED":
                    raise PDFError("PDF_CORRUPT", "文件损坏，读不了") from exc
                raise
            cache.mkdir(parents=True, exist_ok=True)
            run_dir = Path(tempfile.mkdtemp(prefix="pdf-", dir=cache))
            for index in range(count):
                ordinal = index + 1
                page_dir = safe_path(run_dir, f"page-{ordinal}")
                page_dir.mkdir()
                deadline = time.monotonic() + self.page_timeout
                try:
                    page = reader.pages[index]
                    width = float(page.mediabox.width) * float(page.user_unit)
                    height = float(page.mediabox.height) * float(page.user_unit)
                    if page.rotation % 180:
                        width, height = height, width
                    if not all(math.isfinite(n) and n > 0 for n in (width, height)):
                        raise PDFError("PDF_PAGE_INVALID", "页面尺寸无效，读不了")
                    writer = PdfWriter()
                    writer.add_page(page)
                    page_path = page_dir / "page.pdf"
                    with page_path.open("wb") as output:
                        writer.write(output)
                    paths = []
                    for label, dpi in (("preview", self.preview_dpi), ("review", self.review_dpi)):
                        prefix = page_dir / label
                        command = [self.pdftoppm, "-f", "1", "-l", "1", "-singlefile", "-png", "-r", str(dpi)]
                        if max(width, height) * dpi / 72 > self.max_dimension:
                            command += ["-scale-to", str(self.max_dimension)]
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise PDFError("RENDER_TIMEOUT", "渲染超时")
                        self._run(command + [str(page_path), str(prefix)], remaining)
                        image = prefix.with_suffix(".png")
                        if not image.is_file() or image.stat().st_size == 0:
                            raise PDFError("RENDER_FAILED", "页面渲染失败，读不了")
                        paths.append(image.relative_to(cache).as_posix())
                    units.append(PDFUnit("pdf_page", ordinal, f"第 {ordinal} 页", str(ordinal), [],
                                         paths[0], paths[1], page_path.relative_to(cache).as_posix(), width, height))
                except (PDFError, PyPdfError, ValueError, TypeError, KeyError, IndexError, RecursionError) as exc:
                    code = exc.code if isinstance(exc, PDFError) else "PDF_PAGE_CORRUPT"
                    reason = str(exc) if isinstance(exc, PDFError) else "页面损坏，读不了"
                    failures.append(PDFPageFailure(ordinal, code, reason))
                    shutil.rmtree(page_dir)
        return PDFParseResult(units, [f"第 {f.page} 页：{f.reason}" for f in failures], tuple(failures))
