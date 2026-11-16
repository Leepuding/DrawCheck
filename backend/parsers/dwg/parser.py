"""DWG 公共入口；原图只读，文件级状态保留降级原因与 SHA 证据。"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from typing import Iterable

from backend.parsers.base import ParseInput, ParsedText, ParsedUnit, ParseResult
from backend.store.models import BBox, Source
from .libredwg import DwgOptions, discover_tools, run_command
from .status import COMPONENT_UNAVAILABLE, DwgFileResult, DwgFileStatus


def _sha256(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cache_directory(cache: Path, source: Path, sha: str):
    cache = cache.absolute()
    if any(p.is_symlink() for p in (cache, *cache.parents)):
        raise ValueError("cache 路径不可经过符号链接")
    if any(p.name.casefold() == "originals" for p in (cache, *cache.parents)):
        raise ValueError("中间产物禁止写入 originals")
    if cache == source.parent or source.is_relative_to(cache):
        raise ValueError("cache 必须与原图存储分离")
    parent = cache / "dwg" / sha[:16]
    if any(p.is_symlink() for p in (parent, parent.parent)):
        raise ValueError("DWG 缓存子路径不可为符号链接")
    parent.mkdir(parents=True, exist_ok=True)
    return cache, Path(tempfile.mkdtemp(prefix="run-", dir=parent))


def _decode_result(payload):
    units = []
    for unit in payload["units"]:
        texts = [ParsedText(t["text"], Source(t["source"]), BBox(**t["bbox"])) for t in unit["texts"]]
        units.append(ParsedUnit(unit["kind"], unit["ordinal"], unit["name"], unit["drawing_number"],
                                texts, unit["preview_relative_path"], native_measurements=tuple(unit.get("native_measurements", ())),
                                native_signals=tuple(unit.get("native_signals", ())),
                                native_connections=tuple(unit.get("native_connections", ()))))
    return ParseResult(units, payload["incomplete"])


def parse_dwg_file(request: ParseInput, *, options: DwgOptions | None = None) -> DwgFileResult:
    """同步接口供工作线程调用；异步 HTTP 处理器请 await parse_dwg_file_async。"""
    reasons, units, stats = [], [], {}
    before = after = None
    try:
        if request.format != "dwg":
            raise ValueError("DWG 解析器只接受 dwg 格式")
        source = request.readonly_path.resolve(strict=True)
        before = _sha256(source)
        if before != request.sha256.lower():
            raise ValueError("原 DWG 的 SHA-256 与导入记录不一致，未启动解析")
        tools = discover_tools()
        if tools is None:
            reasons.append(COMPONENT_UNAVAILABLE)
        else:
            options = options or DwgOptions.from_env()
            cache, work_dir = _cache_directory(request.cache_dir, source, before)
            payload = {"source": str(source), "work_dir": str(work_dir), "cache": str(cache),
                       "tools": asdict(tools), "options": asdict(options)}
            config = work_dir / "request.json"
            config.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            run = run_command([sys.executable, "-m", "backend.parsers.dwg._worker", str(config)],
                              work_dir, "解析工作进程", options, timeout=options.worker_timeout_sec)
            if run.reason:
                reasons.append(run.reason)
                partial = work_dir / "partial.json"
                if partial.is_file():
                    data = json.loads(partial.read_text(encoding="utf-8"))
                    parsed = _decode_result(data)
                    units, stats = list(parsed.units), data["statistics"]
                    reasons.extend(parsed.incomplete)
            else:
                data = json.loads((work_dir / "result.json").read_text(encoding="utf-8"))
                parsed = _decode_result(data)
                units, reasons, stats = list(parsed.units), list(parsed.incomplete), data["statistics"]
    except Exception as exc:
        reasons.append(f"DWG 解析失败：{exc}")
    finally:
        if before is not None:
            try:
                after = _sha256(request.readonly_path)
                if after != before:
                    reasons.append("原 DWG 解析前后 SHA-256 不一致，结果作废")
                    units = []
            except OSError as exc:
                reasons.append(f"解析后无法复核原 DWG 的 SHA-256，结果作废：{exc}")
                units = []
    reasons = list(dict.fromkeys(reasons))
    status = (DwgFileStatus.UNREADABLE if not units else
              DwgFileStatus.PARTIAL if reasons or any(u.preview_relative_path is None for u in units)
              else DwgFileStatus.SUCCESS)
    reason = "；".join(reasons) if reasons else "已提取图纸单元并生成预览"
    if not units and not reasons:
        reason = "未提取出有效图纸单元"
        reasons.append(reason)
    return DwgFileResult(status, reason, ParseResult(units, reasons), stats, before, after)


async def parse_dwg_file_async(request: ParseInput, *, options: DwgOptions | None = None):
    """连 SHA 与进程等待也交给工作线程，避免阻塞事件循环。"""
    return await asyncio.to_thread(parse_dwg_file, request, options=options)


def parse_dwg_files(requests: Iterable[ParseInput], *, options: DwgOptions | None = None):
    """批量按序执行，避免同批大 JSON 同时占用数百 MB 内存。"""
    return [parse_dwg_file(request, options=options) for request in requests]


class DwgParser:
    def __init__(self, options: DwgOptions | None = None):
        self.options = options

    def parse(self, request: ParseInput) -> ParseResult:
        return parse_dwg_file(request, options=self.options).result
