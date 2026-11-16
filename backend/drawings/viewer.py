"""Resolve source-backed viewer regions in the same normalized sheet space as evidence."""
from __future__ import annotations

from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import dataclass, replace
from functools import lru_cache
import hashlib
import json
import logging
import math
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore, Lock

from PIL import Image

from backend.parsers.dwg.libredwg import DwgOptions, run_command
from backend.store.errors import ErrorCode, StoreError
from backend.store.originals import verify_file
from backend.store.paths import safe_path


_LOGGER = logging.getLogger(__name__)

MAX_DIMENSION = 2048
MAX_PIXELS = 4_194_304
MAX_ZOOM = 1000
# These are coordinate limits, not allocated image sizes.
MAX_VIRTUAL_DIMENSION = 8_388_608
_WORKERS = BoundedSemaphore(2)
_DWG_BUILD_LOCK = Lock()
_DWG_BUILDS = {}
_PDF_TIMEOUT = 25
_DWG_TIMEOUT = 90
_MAX_DWG_RESULT_BYTES = 8 * 1024 * 1024
_PDF_MEMORY_MB = 512
_DWG_PDF_CACHE_VERSION = 2

_PDF_VECTOR_MESSAGE = "高倍区域从 PDF 原页重绘。页内的照片和扫描内容仍受原始像素限制。"
_DWG_VECTOR_MESSAGE = "高倍区域由 DWG 转制的矢量图框重绘。缺失对象、外部参照和字体替换仍需对照原图。"
_RASTER_MESSAGE = "这页按原始栅格图像重绘，清晰度受原文件像素限制。"
_UNAVAILABLE_MESSAGE = "当前图纸没有可用的高清矢量来源，仍可查看已有预览。"


class ViewerError(Exception):
    def __init__(self, code: str, message: str, status: int):
        super().__init__(message)
        self.code, self.status = code, status


@dataclass(frozen=True)
class Region:
    width: int
    height: int
    full_width: int
    full_height: int
    left: int
    top: int

    @classmethod
    def from_query(cls, query) -> Region:
        invalid = ViewerError("INVALID_REGION", "图纸区域或输出尺寸超出允许范围。", 422)
        try:
            x0, y0, x1, y1 = (float(query[key]) for key in ("x0", "y0", "x1", "y1"))
            width, height = (int(query[key]) for key in ("width", "height"))
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise invalid from exc
        if (not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1)
                or not (1 <= width <= MAX_DIMENSION and 1 <= height <= MAX_DIMENSION)
                or width * height > MAX_PIXELS):
            raise invalid
        full_width, full_height = width / (x1 - x0), height / (y1 - y0)
        if not (full_width <= MAX_VIRTUAL_DIMENSION and full_height <= MAX_VIRTUAL_DIMENSION):
            raise invalid
        full_width, full_height = round(full_width), round(full_height)
        # Rounding of the virtual page and origin can differ by a fraction of one output pixel.
        left = min(round(x0 * full_width), full_width - width)
        top = min(round(y0 * full_height), full_height - height)
        return cls(width, height, full_width, full_height, left, top)


@dataclass(frozen=True)
class DrawingSource:
    kind: str
    width: float
    height: float
    message: str
    path: Path | None = None
    ordinal: int = 1
    dwg_frame: tuple | None = None

    def info(self):
        return {"kind": self.kind, "width": self.width, "height": self.height, "message": self.message,
                "max_dimension": MAX_DIMENSION, "max_pixels": MAX_PIXELS, "max_zoom": MAX_ZOOM}


def _fingerprint(path: Path):
    details = path.stat()
    return details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns, details.st_ctime_ns


@lru_cache(maxsize=64)
def _verified(path: Path, sha: str, fingerprint):
    verify_file(path, sha)


def _run_worker(payload, result_path, directory, *, timeout=_PDF_TIMEOUT):
    options = DwgOptions.from_env()
    if payload["operation"] != "dwg":
        options = replace(options, memory_mb=min(options.memory_mb, _PDF_MEMORY_MB))
    request = directory / "request.json"
    request.write_text(json.dumps({**payload, "memory_mb": options.memory_mb},
                                  ensure_ascii=False, allow_nan=False), encoding="utf-8")
    if not _WORKERS.acquire(timeout=2):
        raise ViewerError("VIEWER_BUSY", "高清区域正在绘制，请稍后重试。", 503)
    try:
        result = run_command([sys.executable, "-m", "backend.drawings._viewer_worker", str(request)],
                             directory, "viewer", options, timeout=timeout)
    finally:
        _WORKERS.release()
    if (directory / "memory-limit").is_file() or "MemoryError" in result.stderr_tail:
        raise ViewerError("VIEWER_MEMORY_LIMIT", "图纸区域绘制达到内存上限，请缩小查看范围。", 503)
    if (directory / "no-vector").is_file():
        raise ViewerError("VIEWER_SOURCE_UNAVAILABLE", _UNAVAILABLE_MESSAGE, 409)
    if result.reason or not result_path.is_file():
        _LOGGER.warning("Drawing viewer worker failed (operation=%s, exit=%s)",
                        payload["operation"], result.returncode)
        raise ViewerError("VIEWER_RENDER_FAILED", "高清区域暂时无法绘制，请查看预览或重试。", 503)


@lru_cache(maxsize=64)
def _inspect_pdf(source: Path, ordinal: int, fingerprint):
    with TemporaryDirectory(prefix="drawcheck-viewer-") as directory:
        directory = Path(directory)
        output = directory / "info.json"
        _run_worker({"operation": "inspect", "source": str(source), "ordinal": ordinal}, output, directory)
        return json.loads(output.read_text(encoding="utf-8"))


def _preview_size(preview: Path | None):
    if preview is not None:
        try:
            with Image.open(preview) as image:
                return image.size
        except (OSError, ValueError):
            pass
    return 0, 0


@lru_cache(maxsize=64)
def _frame_from_result(result: Path, fingerprint, ordinal: int, preview_relative: str):
    # The matching parser result was stored with this precise preview, including partially imported runs.
    if fingerprint[2] > _MAX_DWG_RESULT_BYTES:
        return None
    try:
        payload = json.loads(result.read_text(encoding="utf-8"))
        unit = next((item for item in payload["units"] if item.get("ordinal") == ordinal), {})
        stats = payload["statistics"]
        if stats.get("source") != "ezdxf" or unit.get("preview_relative_path") != preview_relative:
            return None
        frame = next(item for item in stats["frame_bounds"] if item.get("ordinal") == ordinal)
        bounds = tuple(float(value) for value in frame["bounds"])
        if (len(bounds) != 4 or not all(math.isfinite(value) for value in bounds)
                or not (bounds[0] < bounds[2] and bounds[1] < bounds[3]) or frame["name"] != unit["name"]):
            return None
        return frame["name"], bounds
    except (ValueError, TypeError, KeyError, AttributeError, StopIteration, OSError, RecursionError):
        return None


def _dwg_source(root: Path, preview: Path | None, ordinal: int, sha: str):
    if preview is None or preview.name != f"unit-{ordinal}.png":
        return None
    run = preview.parent
    if (not run.name.startswith("run-") or run.parent.name != sha[:16]
            or run.parent.parent.name != "dwg"):
        return None
    relative = run.relative_to(root)
    source = safe_path(root, (relative / "drawing.dxf").as_posix())
    if not source.is_file():
        return None
    try:
        with source.open("rb") as stream:
            stream.seek(max(0, source.stat().st_size - 256))
            if not stream.read().replace(b"\r", b"").rstrip().endswith(b"0\nEOF"):
                return None
        for name in ("result.json", "partial.json"):
            result = safe_path(root, (relative / name).as_posix())
            if result.is_file():
                frame = _frame_from_result(result, _fingerprint(result), ordinal,
                                           preview.relative_to(run.parents[2]).as_posix())
                if frame is not None:
                    return source, frame
    except OSError:
        return None
    return None


def resolve_source(repo, unit_id: str) -> DrawingSource:
    unit = repo.get("drawing_units", unit_id)
    original = repo.get("original_files", unit["original_id"])
    if original["version_id"] != unit["version_id"]:
        raise StoreError(ErrorCode.INVALID_DATA, "图纸和原图版本不一致")
    sha = original["sha256"]
    project = original["project_id"]
    metadata = unit.get("metadata") or {}
    relative = metadata.get("review_relative_path") or metadata.get("preview_relative_path")
    cache = safe_path(repo.root, f"projects/{project}/cache")
    preview = safe_path(repo.root, relative) if relative else None
    if preview and not preview.is_relative_to(cache):
        raise StoreError(ErrorCode.INVALID_PATH, "图纸预览路径超出项目缓存")
    width, height = _preview_size(preview)
    unavailable = DrawingSource("unavailable", width, height, _UNAVAILABLE_MESSAGE)

    source = safe_path(repo.root, original["relative_path"])
    if source != safe_path(repo.root, f"projects/{project}/originals/{sha}"):
        raise StoreError(ErrorCode.INVALID_PATH, "图纸原文件路径超出项目原图目录")
    if not source.is_file():
        return unavailable
    fingerprint = _fingerprint(source)
    _verified(source, sha, fingerprint)
    ordinal = unit["ordinal"]
    if unit["kind"] == "pdf_page":
        result = _inspect_pdf(source, ordinal, fingerprint)
        if result["vector"]:
            return DrawingSource("pdf_vector", width or result["width"], height or result["height"],
                                 _PDF_VECTOR_MESSAGE, source, ordinal)
        if result["raster"]:
            return DrawingSource("raster", width or result["width"], height or result["height"],
                                 _RASTER_MESSAGE, source, ordinal)
    if unit["kind"] == "dwg_frame":
        dwg = _dwg_source(repo.root, preview, ordinal, sha)
        if dwg:
            return DrawingSource("dwg_vector", width, height, _DWG_VECTOR_MESSAGE, dwg[0], ordinal, dwg[1])
    return unavailable


def _dwg_pdf(source: DrawingSource):
    dxf = source.path
    fingerprint = _fingerprint(dxf)
    key = hashlib.sha256(repr((_DWG_PDF_CACHE_VERSION, fingerprint, source.dwg_frame)).encode()).hexdigest()[:16]
    pdf = dxf.with_name(f"viewer-{source.ordinal}-{key}.pdf")
    if pdf.is_symlink():
        raise StoreError(ErrorCode.INVALID_PATH, "高清图纸缓存不能经过符号链接")
    with _DWG_BUILD_LOCK:
        if pdf.is_file():
            return pdf
        build = _DWG_BUILDS.get(pdf)
        owner = build is None
        if owner:
            build = _DWG_BUILDS[pdf] = Future()
    if not owner:
        try:
            return build.result(timeout=5)
        except FutureTimeout as exc:
            raise ViewerError("VIEWER_BUSY", "正在准备 DWG 高清图，请稍后重试。", 503) from exc

    try:
        # Build only once per frame and publish atomically. Other frames may use a
        # free rendering slot while this frame is preparing.
        with TemporaryDirectory(prefix="viewer-", dir=dxf.parent) as directory:
            directory = Path(directory)
            output = directory / "sheet.pdf"
            _run_worker({"operation": "dwg", "source": str(dxf), "frame": source.dwg_frame},
                        output, directory, timeout=_DWG_TIMEOUT)
            if fingerprint != _fingerprint(dxf):
                raise ViewerError("VIEWER_SOURCE_CHANGED", "图纸转换缓存已变化，请重新打开图纸。", 409)
            output.replace(pdf)
    except BaseException as exc:
        build.set_exception(exc)
        raise
    else:
        build.set_result(pdf)
        return pdf
    finally:
        with _DWG_BUILD_LOCK:
            _DWG_BUILDS.pop(pdf)


def render_region(source: DrawingSource, region: Region):
    if source.kind == "unavailable":
        raise ViewerError("VIEWER_SOURCE_UNAVAILABLE", source.message, 409)
    path, ordinal = (_dwg_pdf(source), 1) if source.kind == "dwg_vector" else (source.path, source.ordinal)
    with TemporaryDirectory(prefix="drawcheck-viewer-") as directory:
        directory = Path(directory)
        output = directory / "region.png"
        _run_worker({"operation": "region", "source": str(path), "ordinal": ordinal, "region": vars(region)},
                    output, directory)
        # Check bounded output before returning it, independently of the renderer.
        if output.stat().st_size > MAX_PIXELS * 4 + 65_536:
            raise ViewerError("VIEWER_RENDER_FAILED", "高清区域尺寸无效。", 503)
        with Image.open(output) as image:
            if image.format != "PNG" or image.size != (region.width, region.height):
                raise ViewerError("VIEWER_RENDER_FAILED", "高清区域尺寸无效。", 503)
        return output.read_bytes()
