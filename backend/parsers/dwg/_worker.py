"""隔离 DXF/JSON 解析与渲染；转换器崩溃不能直接破坏 API 主进程。"""
from dataclasses import asdict
import json
from pathlib import Path
import re
import sys
import time
from collections import Counter

from backend.parsers.base import ParsedUnit
from .convert import load_complete_dxf
from .frames import find_frames, missing_block_references, read_json_fallback
from .libredwg import DwgOptions, LibreDwgTools, run_command, run_dwg2dxf, run_dwgread_json
from .render import bigfont_diagnostics, render_frame



def scrub_user_reason(text: str) -> str:
    """N2：去掉面向用户原因中的绝对路径，保留文件名；数字与 ERROR 码不动。"""
    def repl(match):
        name = Path(match.group(0)).name
        return name if name else "…"
    stop = r"[^\s;,.，。；：:)\]]"
    return re.sub(rf"(?:[A-Za-z]:)?/(?:{stop}+/)+{stop}+", repl, text)


def memory_reason(stage: str, memory_mb: int) -> str:
    return (f"{stage}时解析内存超过上限 {memory_mb} MB（DRAWCHECK_DWG_MEMORY_MB，Linux 地址空间限额），"
            f"未完成；可调大上限后重试")


def describe_error(exc: BaseException, stage: str, memory_mb: int) -> str:
    """M4：MemoryError 的 str() 为空串，必须显式写明原因；其他空消息异常用类型名兜底。"""
    if isinstance(exc, MemoryError):
        return memory_reason(stage, memory_mb)
    return f"{stage}失败：{exc}" if str(exc) else f"{stage}失败：{type(exc).__name__}"


def json_fallback(path: Path, memory_mb: int):
    """返回 (stats, texts, reasons, ok)；任何失败都给出非空中文原因。"""
    try:
        stats, texts, reasons = read_json_fallback(path)
        return stats, texts, reasons, True
    except MemoryError as exc:
        return {}, [], [describe_error(exc, "LibreDWG JSON 兜底", memory_mb)], False
    except FileNotFoundError:
        # N2：JSON 未产出时不把 cache 绝对路径写进用户可见原因。
        return {}, [], ["LibreDWG JSON 未产出，无法兜底"], False
    except OSError as exc:
        if getattr(exc, "errno", None) == 2:
            return {}, [], ["LibreDWG JSON 未产出，无法兜底"], False
        return {}, [], [describe_error(exc, "LibreDWG JSON 兜底", memory_mb)], False
    except Exception as exc:
        return {}, [], [describe_error(exc, "LibreDWG JSON 兜底", memory_mb)], False


def conversion_warning_counts(path):
    """Read the complete log: its tail can hide early skipped-object warnings."""
    counts = Counter()
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if re.search(r"\b(?:warning|error)\b", line, re.I):
                message = line.strip()
                # Counts are diagnostic evidence, not raw paths in user reasons.
                counts[message[:300]] += 1
    return dict(counts)


def safe_result(payload):
    """LibreDWG can preserve undecodable bytes as lone surrogates. Keep the
    original file untouched; make uncertain extracted text explicit and UTF-8 safe."""
    changed = False
    def clean(value):
        nonlocal changed
        if isinstance(value, str):
            fixed = re.sub(r"[\ud800-\udfff]", "\ufffd", value)
            changed = changed or fixed != value
            return fixed
        if isinstance(value, (list, tuple)):
            return [clean(item) for item in value]
        if isinstance(value, dict):
            return {clean(key): clean(item) for key, item in value.items()}
        return value
    result = clean(payload)
    if changed:
        reason = "部分 CAD 文字编码无法识别，已用替代符标记，请对照原图确认"
        result["incomplete"] = list(dict.fromkeys(result.get("incomplete", []) + [reason]))
    return result


def process(payload):
    source, work_dir = Path(payload["source"]), Path(payload["work_dir"])
    cache = Path(payload["cache"])
    options = DwgOptions(**payload["options"])
    tools = LibreDwgTools(**payload["tools"])
    reasons, units, stats = [], [], {}
    versions = {}
    for name in ("dwg2dxf", "dwgread"):
        check = run_command([getattr(tools, name), "--version"], work_dir,
                            f"{name}-version", options, timeout=min(options.timeout_sec, 5), own_group=False)
        version = (work_dir / f"{name}-version.stdout.log").read_text(errors="replace")[:1000]
        versions[name] = version.strip()
        if check.reason or not re.search(r"\b0\.13\.3\b", version):
            reasons.append(f"{name} 未确认是 Spike 验证的 LibreDWG 0.13.3，兼容性待验证")
    started = time.monotonic()
    timings = {}
    conversion = run_dwg2dxf(tools, source, work_dir / "drawing.dxf", options)
    timings["conversion_seconds"] = round(time.monotonic() - started, 3)
    conversion_warnings = conversion_warning_counts(work_dir / "dwg2dxf.stderr.log")
    if conversion_warnings:
        reasons.append(f"LibreDWG 转换日志含 {sum(conversion_warnings.values())} 条警告或错误，"
                       "可能存在对象遗漏或转换差异，覆盖完整性待原图核对")
    if conversion.reason:
        reasons.append(conversion.reason)
    if any(word in conversion.stderr_tail.lower() for word in ("font", "xref", "proxy", "unsupported")):
        # N2：stderr 原文留在 cache 日志，面向用户只保留短句。
        reasons.append("LibreDWG 提示字体、外参或不支持对象异常，仅部分提取")
    doc = None
    started = time.monotonic()
    try:
        doc = load_complete_dxf(work_dir / "drawing.dxf")
    except MemoryError as exc:
        reasons.append(describe_error(exc, "读取 DXF", options.memory_mb))
    except Exception as exc:
        # N2：转换已失败时，「DXF 无产出/残缺」属于连带噪音，不再重复写入用户原因。
        msg = str(exc) or type(exc).__name__
        if not (conversion.reason and msg in {"DXF 无产出", "DXF 残缺，缺少 EOF"}):
            reasons.append(msg)
    timings["dxf_load_seconds"] = round(time.monotonic() - started, 3)
    font_diagnostics = []
    if doc is not None:
        font_diagnostics = bigfont_diagnostics(doc)
        reasons.extend(row["reason"] for row in font_diagnostics if row["code"] != "FONT_BIGFONT_NOT_USED")
        try:
            started = time.monotonic()
            frames, frame_reasons, counts = find_frames(doc)
            timings["frame_detection_seconds"] = round(time.monotonic() - started, 3)
            reasons.extend(frame_reasons)
            stats = {"source": "ezdxf", "entities": counts, "layout_count": len(doc.layouts),
                     "entity_total": sum(counts.values()),
                     "timings": timings, "conversion_warnings": conversion_warnings,
                     "font_diagnostics": font_diagnostics,
                     "frame_bounds": [{"ordinal": n, "name": frame.name, "bounds": list(frame.bounds)}
                                      for n, frame in enumerate(frames, 1)],
                     "missing_block_references": [
                         {"handle": e.dxf.handle, "name": e.dxf.name,
                          "insert": list(e.dxf.insert)} for e in missing_block_references(doc)],
                     "external_references": [
                         {"block": block.name, "path": block.block.dxf.get("xref_path", ""),
                          "flags": block.block.dxf.flags, "cached_entity_count": len(block)}
                         for block in doc.blocks if block.block.dxf.flags & 12],
                     "unflagged_block_path_count": sum(
                         bool(block.block.dxf.get("xref_path", "")) and not block.block.dxf.flags & 12
                         for block in doc.blocks)}
        except MemoryError as exc:
            frames = []
            reasons.append(describe_error(exc, "DXF 布局或图框读取", options.memory_mb) + "，尝试 JSON 兜底")
        except Exception as exc:
            frames = []
            reasons.append(f"DXF 布局或图框读取失败，尝试 JSON 兜底：{exc or type(exc).__name__}")
        # Persist every discovered frame before rendering. A timed-out process must not
        # discard completed images or silently omit frames that have not rendered yet.
        frame_units = [ParsedUnit("dwg_frame", n, frame.name, "", frame.texts, None, native_measurements=frame.native_measurements, native_signals=frame.native_signals, native_connections=frame.native_connections)
                       for n, frame in enumerate(frames, 1)]
        def checkpoint():
            pending_count = sum(u.preview_relative_path is None for u in frame_units)
            partial = {"units": [asdict(u) for u in frame_units],
                       "incomplete": list(dict.fromkeys(scrub_user_reason(r) for r in reasons)) +
                           ([f"另有 {pending_count} 张图渲染未完成"] if pending_count else []),
                       "statistics": {**stats, "frame_count": len(frame_units), "libredwg_versions": versions}}
            temp = work_dir / "partial.tmp"
            temp.write_text(json.dumps(safe_result(partial), ensure_ascii=False), encoding="utf-8")
            temp.replace(work_dir / "partial.json")
        if frame_units:
            checkpoint()
        for ordinal, frame in enumerate(frames, 1):
            started = time.monotonic()
            preview = work_dir / f"unit-{ordinal}.png"
            relative = None
            try:
                reasons.extend(render_frame(doc, frame, preview))
                if not preview.is_file() or preview.stat().st_size == 0:
                    raise ValueError("未生成 PNG")
                relative = preview.relative_to(cache).as_posix()
            except MemoryError as exc:
                reasons.append(describe_error(exc, f"单元 {ordinal} 渲染", options.memory_mb))
            except Exception as exc:
                reasons.append(f"单元 {ordinal} 渲染不完整：{exc or type(exc).__name__}")
            # 不从文件名或 TK-* 块名推断正式图号。
            parsed_unit = ParsedUnit("dwg_frame", ordinal, frame.name, "", frame.texts, relative, native_measurements=frame.native_measurements, native_signals=frame.native_signals, native_connections=frame.native_connections)
            units.append(parsed_unit)
            frame_units[ordinal-1] = parsed_unit
            timings.setdefault("render_seconds", []).append(round(time.monotonic()-started, 3))
            checkpoint()
    if not units:
        conversion = run_dwgread_json(tools, source, work_dir / "drawing.json", options)
        if conversion.reason:
            reasons.append(conversion.reason)
        json_stats, texts, json_reasons, ok = json_fallback(work_dir / "drawing.json", options.memory_mb)
        # N2：dwgread 已失败且 JSON 未产出时，跳过级联「未产出」短句，避免重复。
        if conversion.reason and json_reasons == ["LibreDWG JSON 未产出，无法兜底"]:
            pass
        else:
            reasons.extend(json_reasons)
        if ok:
            stats = json_stats
            if stats["entity_total"]:
                units.append(ParsedUnit("dwg_frame", 1, "整图（JSON 部分提取，图框待确认）", "", texts))
            else:
                reasons.append("LibreDWG JSON 无有效实体")
    stats["libredwg_versions"] = versions
    stats["timings"] = timings
    stats["conversion_warnings"] = conversion_warnings
    stats["font_diagnostics"] = font_diagnostics
    reasons = list(dict.fromkeys(scrub_user_reason(r) for r in reasons))
    if not units:
        reasons.append("LibreDWG 未提取出有效图纸单元")
    return safe_result({"units": [asdict(u) for u in units], "incomplete": reasons, "statistics": stats})


def main():
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    result = process(payload)
    (Path(payload["work_dir"]) / "result.json").write_text(
        json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
